"""Changing a schedule's agent moves the template it runs, not only the label."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import patch

import pytest

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.cron import CronService, resolve_cron_memory
from kiro_crew.cron_service.identity import rebind_cron_session_template
from kiro_crew.execution_context import (
    bind_session_execution,
    execution_from_record,
    read_session_execution,
)
from kiro_crew.memory_stores import provision_member_memory


def _capture(job):
    return execution_from_record({"execution_context": job.execution_context})


def _reloaded(tmp_path, job_id):
    return CronService(base_dir=tmp_path / "cron").get_job(job_id)


def test_update_agent_moves_the_dispatched_template(tmp_path):
    service = CronService(base_dir=tmp_path / "cron")
    job = service.add_job("daily", "task", every_secs=60, agent_id="alpha")
    assert resolve_cron_memory(job)[1] == "alpha"

    service.update_job(job.id, agent_id="beta")

    stored = _reloaded(tmp_path, job.id)
    assert stored.agent_id == "beta"
    assert resolve_cron_memory(stored)[1] == "beta"


def test_update_agent_keeps_store_and_mode_of_the_capture(tmp_path):
    service = CronService(base_dir=tmp_path / "cron")
    job = service.add_job("daily", "task", every_secs=60, agent_id="alpha")
    before = _capture(job)

    service.update_job(job.id, agent_id="beta")

    after = _capture(_reloaded(tmp_path, job.id))
    assert (after.store, after.member_id, after.memory_mode, after.app) == (
        before.store,
        before.member_id,
        before.memory_mode,
        before.app,
    )


def test_clearing_the_agent_returns_to_the_default_floor(tmp_path):
    service = CronService(base_dir=tmp_path / "cron")
    job = service.add_job("daily", "task", every_secs=60, agent_id="alpha")

    service.update_job(job.id, agent_id="")

    assert resolve_cron_memory(_reloaded(tmp_path, job.id))[1] == "kirocrew"


def test_resubmitting_the_same_agent_leaves_the_capture_alone(tmp_path):
    service = CronService(base_dir=tmp_path / "cron")
    job = service.add_job("daily", "task", every_secs=60)
    # A schedule made from a template chat names its template only in the capture.
    job.execution_context = {**job.execution_context, "template_id": "from-chat"}
    service._save()

    service.update_job(job.id, name="renamed", agent_id="")

    stored = _reloaded(tmp_path, job.id)
    assert stored.name == "renamed"
    assert resolve_cron_memory(stored)[1] == "from-chat"


def _fire_rebind(job):
    """What a fire does before its plain publish of the capture."""
    rebind_cron_session_template(f"cron:{job.id}", _capture(job), job.name)
    bind_session_execution(f"cron:{job.id}", _capture(job))


def test_next_run_after_agent_change_publishes_the_new_template(tmp_path):
    service = CronService(base_dir=tmp_path / "cron")
    job = service.add_job("daily", "task", every_secs=60, agent_id="alpha")
    key = f"cron:{job.id}"
    bind_session_execution(key, _capture(job))  # what the previous run published

    service.update_job(job.id, agent_id="beta")
    _fire_rebind(_reloaded(tmp_path, job.id))

    assert read_session_execution(key).template_id == "beta"


def test_a_record_published_after_the_update_is_caught_by_the_next_run(tmp_path):
    service = CronService(base_dir=tmp_path / "cron")
    job = service.add_job("daily", "task", every_secs=60, agent_id="alpha")
    key = f"cron:{job.id}"
    old_capture = _capture(job)

    service.update_job(job.id, agent_id="beta")
    # A run still in flight during the update publishes its older capture late.
    bind_session_execution(key, old_capture)
    _fire_rebind(_reloaded(tmp_path, job.id))

    assert read_session_execution(key).template_id == "beta"


def test_agent_change_while_stateless_is_caught_once_persistence_returns(tmp_path):
    service = CronService(base_dir=tmp_path / "cron")
    job = service.add_job("daily", "task", every_secs=60, agent_id="alpha")
    key = f"cron:{job.id}"
    bind_session_execution(key, _capture(job))

    service.update_job(job.id, persistent_session=False)
    service.update_job(job.id, agent_id="beta")
    service.update_job(job.id, persistent_session=True)
    _fire_rebind(_reloaded(tmp_path, job.id))

    assert read_session_execution(key).template_id == "beta"


def test_a_session_record_naming_another_execution_is_left_alone(tmp_path):
    service = CronService(base_dir=tmp_path / "cron")
    job = service.add_job("daily", "task", every_secs=60, agent_id="alpha")
    key = f"cron:{job.id}"
    other = replace(_capture(job), app="some-app")
    bind_session_execution(key, other)

    service.update_job(job.id, agent_id="beta")
    rebind_cron_session_template(key, _capture(_reloaded(tmp_path, job.id)), job.name)

    assert read_session_execution(key) == other


def test_an_unchanged_record_is_not_rewritten(tmp_path):
    service = CronService(base_dir=tmp_path / "cron")
    job = service.add_job("daily", "task", every_secs=60, agent_id="alpha")
    key = f"cron:{job.id}"
    bind_session_execution(key, _capture(job))

    with patch("kiro_crew.execution_context.bind_session_execution") as bind:
        rebind_cron_session_template(key, _capture(job), job.name)

    bind.assert_not_called()


@pytest.fixture
def writer_member():
    cfg = KiroCrewConfig.load()
    cfg.agents["writer"] = KiroCrewAgentConfig(kiro_agent="writer-template", triggers="write")
    store = provision_member_memory(cfg, "writer")
    cfg.save()
    return store


def test_member_schedule_agent_change_keeps_the_member(tmp_path, writer_member):
    service = CronService(base_dir=tmp_path / "cron")
    job = service.add_job("daily", "task", every_secs=60, member_id="writer", agent_id="alpha")
    assert resolve_cron_memory(job) == (writer_member, "alpha")

    service.update_job(job.id, agent_id="beta")
    assert resolve_cron_memory(_reloaded(tmp_path, job.id)) == (writer_member, "beta")

    service.update_job(job.id, agent_id="")
    stored = _reloaded(tmp_path, job.id)
    assert resolve_cron_memory(stored) == (writer_member, "writer-template")
    assert _capture(stored).member_id == _capture(job).member_id
