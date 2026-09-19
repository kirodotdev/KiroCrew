"""Canonical execution routing is independent of labels and parent lifetime."""

from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace

import pytest

from kiro_crew import execution_context as execution
from kiro_crew.config.sections import KiroCrewAgentConfig, MemoryStoreConfig
from kiro_crew.memory_stores import UnknownMemoryStore
from kiro_crew.vector_memory import create_member_database


@pytest.fixture
def members(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    from kiro_crew.config import loader

    loader._invalidate_config_cache()
    execution._LIVE_EXECUTIONS.clear()
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


def test_database_identity_path_and_cross_member_routing(members):
    alice = execution.resolve_member_execution(members, "alice")
    bob = execution.resolve_member_execution(members, "bob")
    assert alice.template_id == bob.template_id
    assert alice.member_id != bob.member_id
    assert execution.validate_execution(alice) == alice
    assert execution.validate_execution(bob) == bob
    with pytest.raises(FrozenInstanceError):
        alice.member_id = bob.member_id


def test_rename_and_rebind_cannot_reinterpret_captured_work(members):
    admitted = execution.resolve_member_execution(members, "alice")
    members.agents["renamed"] = members.agents.pop("alice")
    members.agents["renamed"].memory_store = "member-bob"
    assert execution.derive_execution(admitted) is not None
    assert execution.validate_execution(admitted).store.store_id == "member-alice"
    assert execution.member_config_for_id(members, "id-alice")[0] == "renamed"


def test_explicit_target_selects_existing_member_without_broadening_privacy(members):
    parent = execution.resolve_member_execution(
        members, "alice", memory_mode="incognito", app="example-app"
    )
    inherited = execution.derive_execution(parent, requested_mode="persistent")
    targeted = execution.derive_execution(parent, target_member="bob", config=members)
    assert inherited == parent
    assert targeted.member_id == "id-bob"
    assert targeted.memory_mode == "incognito"
    assert targeted.app == "example-app"
    with pytest.raises(UnknownMemoryStore):
        execution.derive_execution(parent, target_member="missing", config=members)


def test_corrupt_member_never_becomes_global(members, tmp_path):
    admitted = execution.resolve_member_execution(members, "alice")
    (tmp_path / "memory_stores" / "member-alice" / "memory.db").unlink()
    with pytest.raises((ValueError, OSError)):
        execution.validate_execution(admitted)
    assert admitted.store.store_id == "member-alice"


@pytest.mark.parametrize("admission", ["captured", "configured"])
def test_member_admission_normalizes_selected_sqlite_driver_errors(members, monkeypatch, admission):
    from kiro_crew import vector_memory
    from kiro_crew.memory_stores import require_memory_store

    admitted = execution.resolve_member_execution(members, "alice")

    class AlternateDriverError(Exception):
        """A driver error unrelated to stdlib sqlite3.Error, like pysqlite3's."""

    failure = AlternateDriverError("synthetic unreadable member database")

    opened = []

    def connect(database, *, uri):
        opened.append((database, uri))
        raise failure

    monkeypatch.setattr(
        vector_memory, "sqlite3", SimpleNamespace(Error=AlternateDriverError, connect=connect)
    )
    with pytest.raises(UnknownMemoryStore, match="unreadable") as refused:
        if admission == "captured":
            execution.validate_execution(admitted)
        else:
            require_memory_store("member-alice", config=members)
    assert refused.value.__cause__ is failure
    assert "Global was not used" in str(refused.value)
    from kiro_crew.memory_stores import _named_store_dir

    path = _named_store_dir("member-alice") / "memory.db"
    assert opened == [(path.resolve().as_uri() + "?mode=ro", True)]
    assert admitted.store.store_id == "member-alice"


def test_restricted_session_record_stays_live_and_monotonic(members, tmp_path):
    admitted = execution.resolve_member_execution(members, "alice", memory_mode="incognito")
    execution.bind_session_execution("dashboard_private", admitted)
    assert execution.read_session_execution("dashboard_private") == admitted
    execution.bind_session_execution(
        "dashboard_private", replace(admitted, memory_mode="persistent")
    )
    assert execution.read_session_execution("dashboard_private").memory_mode == "incognito"
    from kiro_crew.history import ConversationLog

    assert not ConversationLog()._path("dashboard_private").exists()


def test_session_publication_compares_captured_record(members):
    alice = execution.resolve_member_execution(members, "alice")
    bob = execution.resolve_member_execution(members, "bob")
    execution.bind_session_execution("dashboard_race", alice)
    with pytest.raises(UnknownMemoryStore):
        execution.bind_session_execution(
            "dashboard_race", bob, replace_existing=True, expected=None
        )
    assert execution.read_session_execution("dashboard_race") == alice


def test_missing_canonical_member_record_is_an_error(members):
    from kiro_crew.history import ConversationLog

    ConversationLog().update_metadata("dashboard_damaged", {"memory_store": "member-alice"})
    with pytest.raises(UnknownMemoryStore):
        execution.read_session_execution("dashboard_damaged")


def test_subagent_owns_context_after_parent_disappears(members):
    from kiro_crew.subagent_persistence import create_agent_folder, read_run_execution

    parent = execution.resolve_member_execution(members, "alice")
    create_agent_folder("test-child", task="synthetic", execution_context=parent)
    members.agents.clear()
    child = read_run_execution("test-child")
    assert child == parent
    assert execution.validate_execution(child) == parent


def test_restricted_subagent_does_not_write_task_or_result(members):
    from kiro_crew.subagent_persistence import (
        create_agent_folder,
        read_run_execution,
        write_result_chunk,
    )

    parent = execution.resolve_member_execution(members, "alice", memory_mode="temporary")
    directory = create_agent_folder(
        "test-restricted",
        task="synthetic private prompt",
        execution_context=parent,
        memory_mode="temporary",
    )
    write_result_chunk("test-restricted", "synthetic private result")
    assert read_run_execution("test-restricted") == parent
    assert not directory.exists()


def test_cron_record_keeps_member_after_parent_and_config_change(members):
    from kiro_crew.cron import CronJob, CronSchedule, bind_cron_memory, resolve_cron_memory

    parent = execution.resolve_member_execution(members, "alice")
    execution.bind_session_execution("dashboard_cron", parent)
    job = CronJob(
        id="job-a",
        name="synthetic",
        message="test",
        schedule=CronSchedule(kind="every", every_secs=60),
        session_key="dashboard_cron",
    )
    bind_cron_memory(job)
    members.agents.clear()
    assert resolve_cron_memory(job) == ("member-alice", "shared-template")


@pytest.mark.asyncio
async def test_workflow_carrier_survives_closed_parent_and_uses_single_record(members, tmp_path):
    from kiro_crew.workflow_memory import WorkflowScope

    parent = execution.resolve_member_execution(members, "alice")
    execution.bind_session_execution("dashboard_workflow", parent)
    context = SimpleNamespace(_session_memory_modes={})
    scope = await WorkflowScope.admit("wf_test", context, "dashboard_workflow")
    members.agents.clear()
    await scope.prepare(context, scope.worker_key("worker"))
    assert scope.execution_context == parent
    assert execution.read_session_execution(scope.worker_key("worker")) == parent
    assert not (tmp_path / "member-memory-bindings").exists()


def test_repeated_retention_tightening_survives_restart_without_new_body(members):
    from kiro_crew.history import ConversationLog

    admitted = execution.resolve_member_execution(members, "alice")
    key = "dashboard_restricted_restart"
    execution.bind_session_execution(key, admitted)
    log = ConversationLog()
    original_rows = log._path(key).read_text(encoding="utf-8").splitlines()[1:]
    execution.bind_session_execution(key, admitted.with_mode("incognito"))
    execution.bind_session_execution(key, admitted.with_mode("temporary"))
    execution._LIVE_EXECUTIONS.clear()
    restored = execution.read_session_execution(key, required=True)
    assert restored.memory_mode == "temporary"
    assert restored.store == admitted.store
    assert log._path(key).read_text(encoding="utf-8").splitlines()[1:] == original_rows


def test_cancelled_restricted_selection_preserves_strongest_retention(members):
    admitted = execution.resolve_member_execution(members, "alice", memory_mode="incognito")
    published = execution.resolve_member_execution(members, "bob", memory_mode="temporary")
    execution.bind_session_execution("dashboard_cancelled", admitted)
    execution.bind_session_execution("dashboard_cancelled", published, replace_existing=True)
    assert execution.restore_live_session_execution(
        "dashboard_cancelled", admitted.to_record(), published.to_record()
    )
    restored = execution.read_session_execution("dashboard_cancelled", required=True)
    assert restored.member_id == admitted.member_id
    assert restored.memory_mode == "temporary"


def test_old_close_cannot_clear_reused_session_identity(members):
    old = execution.resolve_member_execution(members, "alice", memory_mode="incognito")
    new = execution.resolve_member_execution(members, "bob", memory_mode="incognito")
    key = "dashboard:reused-key"
    execution.bind_session_execution(key, old)
    execution.bind_session_execution(key, new, replace_existing=True)
    execution.clear_session_execution(key, expected=old)
    assert execution.read_session_execution(key) == new
    execution.clear_session_execution(key, expected=new)
    assert execution.read_session_execution(key) is None
