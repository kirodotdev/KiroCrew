"""An owner update that names a crew member in ``agent`` is REFUSED, not
recorded as a non-resolving template.

Re-pointing an existing schedule at a crew member is not supported: it would
change the capture's store and identity under the schedule's stable
``cron:<id>`` session key, which the first run binds and the next fire's rebind
cannot reconcile. Rather than silently record the member name as a template the
dispatch reply path cannot resolve -- the create-path breakage this test guards
against -- the three owner update callers (CLI ``cron update``, dashboard
``PATCH /api/crons``, MCP ``cron_update``) refuse it with a clear message, and
the create paths remain the supported way to run a schedule as a member.

An app-owned update is not classified at all (the App Kit SDK forwards
``agent_id`` after only an ownership check), so an app naming a member stays
template-only, exactly as on main. A plain provider template name is re-pointed.
"""

from __future__ import annotations

import argparse
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.cli_commands import _cron
from kiro_crew.config.loader import (
    KiroCrewAgentConfig,
    KiroCrewConfig,
    config_dir,
)
from kiro_crew.cron import CronService
from kiro_crew.dashboard.handlers.cron import api_cron_update
from kiro_crew.execution_context import execution_from_record
from kiro_crew.memory_stores import provision_member_memory


@pytest.fixture
def member_with_distinct_template(tmp_path, monkeypatch):
    """A crew member whose display name differs from its kiro template."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    (config_dir() / "crons").mkdir(parents=True)
    cfg = KiroCrewConfig.load()
    cfg.agents["SampleMemberAgent"] = KiroCrewAgentConfig(
        kiro_agent="sample-template-agent", triggers="sample"
    )
    provision_member_memory(cfg, "SampleMemberAgent")
    cfg.save()
    return KiroCrewConfig.load().agents["SampleMemberAgent"].member_id


def _svc() -> CronService:
    return CronService(base_dir=config_dir())


def _only_job():
    return _svc().list_jobs(include_disabled=True)[0]


def _capture(job):
    return execution_from_record({"execution_context": job.execution_context})


def _template_job() -> str:
    """A plain template schedule (no agent -> default template). Returns its id."""
    job = _svc().add_job(name="daily", message="task", every_secs=60)
    cap0 = _capture(_only_job())
    assert cap0.selection_kind == "template"
    assert cap0.member_id is None
    return job.id


def _assert_untouched_template() -> None:
    job = _only_job()
    assert job.member_id == ""
    cap = _capture(job)
    assert cap.selection_kind == "template"
    assert not cap.member_id


def _update_ns(job_id: str, **overrides) -> argparse.Namespace:
    base = dict(
        cron_action="update",
        job_id=job_id,
        name=None,
        message=None,
        every_secs=None,
        cron_expr=None,
        channel=None,
        timeout_secs=None,
        agent=None,
        approval_mode=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


# ── Owner update naming a member is REFUSED on every owner path ──────────────


def test_update_via_service_to_member_name_is_refused(member_with_distinct_template):
    job_id = _template_job()
    with pytest.raises(ValueError, match="isn't supported"):
        _svc().update_job(job_id, agent_id="SampleMemberAgent")
    _assert_untouched_template()


def test_update_via_cli_to_member_name_is_refused(member_with_distinct_template):
    job_id = _template_job()
    with pytest.raises(SystemExit):
        _cron(_update_ns(job_id, agent="SampleMemberAgent"))
    _assert_untouched_template()


def test_update_via_mcp_to_member_name_is_refused(member_with_distinct_template):
    from kiro_crew.mcp_cron import _call_tool_locally

    with patch("kiro_crew.mcp_cron._authz_session_key", return_value="dashboard:tester"):
        _call_tool_locally("cron_add", {"name": "daily", "message": "task", "every": 60})
        job_id = _only_job().id
        result = _call_tool_locally("cron_update", {"job_id": job_id, "agent": "SampleMemberAgent"})
    assert "Error" in result
    assert "isn't supported" in result
    _assert_untouched_template()


def _dashboard_app(update_job_async) -> web.Application:
    app = web.Application()
    app["state"] = SimpleNamespace(
        crons=SimpleNamespace(update_job_async=update_job_async),
        push_refresh=MagicMock(),
    )
    app.router.add_route("PATCH", "/api/crons/{job_id}", api_cron_update)
    return app


@pytest.mark.asyncio
async def test_update_via_dashboard_to_member_name_is_refused(member_with_distinct_template):
    svc = _svc()
    job_id = _template_job()
    app = _dashboard_app(svc.update_job_async)
    with patch(
        "kiro_crew.dashboard.handlers.cron.require_owner_dashboard_request",
        return_value=None,
    ):
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch(f"/api/crons/{job_id}", json={"agent": "SampleMemberAgent"})
    assert resp.status == 400
    _assert_untouched_template()


# ── A plain template name is re-pointed, as before ───────────────────────────


def test_update_to_a_plain_template_name_stays_a_template(member_with_distinct_template):
    job_id = _template_job()
    _cron(_update_ns(job_id, agent="some-other-template"))
    job = _only_job()
    assert job.member_id == ""
    assert job.agent_id == "some-other-template"
    cap = _capture(job)
    assert cap.selection_kind == "template"
    assert cap.template_id == "some-other-template"
    assert not cap.member_id


# ── App Kit SDK update naming a member stays template-only (unchanged) ───────


def test_sdk_update_job_naming_a_member_stays_a_template(member_with_distinct_template):
    """An app editing its own cron's agent to a member NAME is not classified at
    all: it stays a template and reaches only global memory, exactly as on main.
    An app is never even offered the member re-point, so it is not refused — it
    is simply recorded as the (template) name it gave."""
    from kiro_crew.apps.cron_sdk import CronSDK

    sdk = CronSDK("testapp", _svc())
    job = sdk.add_job("daily", "task", every_secs=60)
    sdk.update_job(job.id, agent_id="SampleMemberAgent")

    updated = _only_job()
    assert updated.member_id == ""
    assert updated.agent_id == "SampleMemberAgent"
    cap = _capture(updated)
    assert cap.selection_kind == "template"
    assert not cap.member_id
    assert cap.store.store_id == "default"
