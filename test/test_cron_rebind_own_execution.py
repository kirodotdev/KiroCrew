"""A cron job's stable session key accepts the job's own new dispatch identity.

The single-agent fire binds its dispatch identity with a plain
``bind_session_execution``, which refuses a record that differs. A project-bound
job's dispatch identity moves when its checkout starts or stops declaring the
dispatched template (store: captured <-> Global) or the binding moves the
template. The gateway resets the live session for that but nothing rewrote the
durable record, so every later fire raised "session already belongs to another
execution". ``rebind_own_cron_execution`` replaces the record first -- but only
a record this job itself could have published.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew import execution_context as execution
from kiro_crew.config.sections import KiroCrewAgentConfig, MemoryStoreConfig
from kiro_crew.cron_service.identity import rebind_own_cron_execution
from kiro_crew.execution_context import (
    bind_session_execution,
    execution_for_project_override,
    read_session_execution,
)
from kiro_crew.memory_stores import MissingExecutionIdentity
from kiro_crew.vector_memory import create_member_database

KEY = "cron:job-1"


@pytest.fixture
def members(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    from kiro_crew.config import loader

    loader._invalidate_config_cache()
    execution._LIVE_EXECUTIONS.clear()
    execution._VOUCHED_EXECUTIONS.clear()
    cfg = SimpleNamespace(agents={}, memory_stores={})
    for name in ("alice", "bob"):
        store = f"member-{name}"
        path = tmp_path / "memory_stores" / store / "memory.db"
        path.parent.mkdir(parents=True)
        create_member_database(path, member_id=f"id-{name}", store_id=store)
        cfg.agents[name] = KiroCrewAgentConfig(
            member_id=f"id-{name}", memory_store=store, kiro_agent="shared-template"
        )
        cfg.memory_stores[store] = MemoryStoreConfig(
            owner_member=name, owner_member_id=f"id-{name}", memory_version=2
        )
    monkeypatch.setattr(loader.KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    yield cfg
    execution._LIVE_EXECUTIONS.clear()
    execution._VOUCHED_EXECUTIONS.clear()


def _alice(members):
    return execution.resolve_member_execution(members, "alice")


def test_the_plain_bind_refuses_a_moved_identity(members):
    """The failure the gateway hit before the rebind: pinned so the test below means something."""
    job = _alice(members)
    bind_session_execution(KEY, job)
    moved = execution_for_project_override(job, template_id="project-template")
    with pytest.raises(Exception, match="another execution"):
        bind_session_execution(KEY, moved)


def test_the_project_override_replaces_the_captured_record(members):
    job = _alice(members)
    bind_session_execution(KEY, job)
    moved = execution_for_project_override(job, template_id="project-template")

    assert rebind_own_cron_execution(KEY, job, moved) is True
    assert read_session_execution(KEY) == moved
    # The acquisition's own plain bind now sees an equal record and passes.
    bind_session_execution(KEY, moved)


def test_the_capture_replaces_a_stale_project_override(members):
    """The checkout stopped declaring the template, or the folder was unbound."""
    job = _alice(members)
    bind_session_execution(KEY, execution_for_project_override(job, template_id="project-template"))

    assert rebind_own_cron_execution(KEY, job, job) is True
    assert read_session_execution(KEY) == job


def test_a_template_move_on_the_same_store_is_replaced(members):
    job = _alice(members)
    bind_session_execution(KEY, job)
    moved = job.with_template("other-template", "other-template")

    assert rebind_own_cron_execution(KEY, job, moved) is True
    assert read_session_execution(KEY) == moved


def test_another_members_record_is_left_for_the_bind_to_refuse(members):
    job = _alice(members)
    bob = execution.resolve_member_execution(members, "bob")
    bind_session_execution(KEY, bob)

    assert rebind_own_cron_execution(KEY, job, job) is False
    assert read_session_execution(KEY) == bob


def test_another_apps_record_is_left_alone(members):
    from dataclasses import replace

    job = _alice(members)
    bind_session_execution(KEY, replace(job, app="some-app"))

    assert rebind_own_cron_execution(KEY, job, job) is False


def test_a_matching_or_absent_record_is_not_rewritten(members):
    job = _alice(members)
    assert rebind_own_cron_execution(KEY, job, job) is False
    assert read_session_execution(KEY) is None
    bind_session_execution(KEY, job)
    assert rebind_own_cron_execution(KEY, job, job) is False


def test_an_unattributable_record_is_not_guessed(members, monkeypatch):
    def _missing(_key):
        raise MissingExecutionIdentity("no carrier")

    monkeypatch.setattr(execution, "read_session_execution", _missing)
    job = _alice(members)
    assert rebind_own_cron_execution(KEY, job, job) is False
