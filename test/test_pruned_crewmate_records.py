"""Records bound to a crewmate the startup prune removed stay executable.

``crewmate_prune_migration`` deletes the ``config.agents`` rows an older agent
sync generated and leaves each agent installed. Chats, forks, subagent runs and
cron jobs that picked one of those crewmates still carry a ``member`` record
naming it, and a reader that trusts the kind refuses it as an unavailable
member. The record decoder re-reads exactly that shape as its template on
the shared store -- but only for a name the prune's own marker lists as
removed -- so every reader gets the same answer.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from kiro_crew import execution_context as ec
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.subagent_manager.admission.gate import _GateMixin


def _config(*names: str) -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.agents = {name: KiroCrewAgentConfig(kiro_agent=name) for name in names}
    return cfg


def _record(
    *, name="synced-agent", template="synced-agent", store="default", member_id=None
) -> dict:
    execution = ec.ExecutionContext(
        member_id,
        ec.MemoryStoreRef(store, member_id),
        "member",
        template,
        selection_name=name,
    )
    return {ec.EXECUTION_CONTEXT_KEY: execution.to_record()}


@pytest.fixture
def config(monkeypatch):
    holder = {"cfg": _config("kirocrew"), "removed": frozenset({"synced-agent"})}
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls, *a, **k: holder["cfg"]))
    monkeypatch.setattr(
        "kiro_crew.crewmate_prune_migration.removed_crewmate_names", lambda: holder["removed"]
    )
    return holder


def test_decoder_reads_a_pruned_synced_crewmate_as_its_template(config):
    execution = ec.execution_from_record(_record())
    assert execution.selection_kind == "template"
    assert execution.template_id == "synced-agent"
    assert execution.selection_name == "synced-agent"
    assert execution.store.store_id == "default"
    assert execution.member_id is None


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"store": "private-store"}, id="own-store"),
        pytest.param({"store": "member-store", "member_id": "m-1"}, id="identity"),
        pytest.param({"name": "renamed", "template": "synced-agent"}, id="name-differs"),
    ],
)
def test_decoder_keeps_every_other_member_record(config, kwargs):
    execution = ec.execution_from_record(_record(**kwargs))
    assert execution.selection_kind == "member"


def test_decoder_keeps_a_member_whose_row_still_exists(config):
    config["cfg"] = _config("kirocrew", "synced-agent")
    assert ec.execution_from_record(_record()).selection_kind == "member"


def test_decoder_keeps_a_member_deleted_by_any_other_route(config):
    """A member row deleted by hand, not by the prune, keeps refusing."""
    config["removed"] = frozenset()
    assert ec.execution_from_record(_record()).selection_kind == "member"


def test_decoder_keeps_the_record_when_config_is_unreadable(config, monkeypatch):
    def boom(cls, *a, **k):
        raise OSError("unreadable")

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(boom))
    assert ec.execution_from_record(_record()).selection_kind == "member"


def test_decoder_keeps_the_record_when_the_marker_is_unreadable(config, monkeypatch):
    def boom():
        raise OSError("unreadable")

    monkeypatch.setattr("kiro_crew.crewmate_prune_migration.removed_crewmate_names", boom)
    assert ec.execution_from_record(_record()).selection_kind == "member"


def test_continued_subagent_run_of_a_pruned_crewmate_is_admitted(config, monkeypatch):
    """``spawn_continue`` admits it rather than "selected member is unavailable"."""
    run = ec.execution_from_record(_record())
    gate = SimpleNamespace()
    execution = _GateMixin.resolve_spawn_execution(
        gate, conversation_key="subagent:run-1", _record=run
    )
    assert execution.selection_kind == "template"
    assert execution.template_id == "synced-agent"


def test_spawn_inheriting_a_pruned_crewmate_keeps_its_template(config):
    gate = SimpleNamespace()
    execution = _GateMixin.resolve_spawn_execution(
        gate,
        parent_session_key="dashboard:old-chat",
        _record=None,
        _inherited_selection=("member", "synced-agent"),
    )
    assert execution.selection_kind == "template"
    assert execution.template_id == "synced-agent"
    assert execution.store.store_id == "default"


def test_spawn_inheriting_a_member_the_prune_did_not_remove_still_refuses(config):
    config["removed"] = frozenset()
    with pytest.raises(ValueError, match="selected member is unavailable"):
        _GateMixin.resolve_spawn_execution(
            SimpleNamespace(),
            parent_session_key="dashboard:old-chat",
            _record=None,
            _inherited_selection=("member", "synced-agent"),
        )


def test_spawn_inheriting_a_missing_member_on_its_own_store_still_refuses(config, monkeypatch):
    def store_exists(store, *, memory_mode="persistent", app="", template_id=""):
        return ec.ExecutionContext(
            None, ec.MemoryStoreRef(store), "template", template_id, memory_mode, app
        )

    monkeypatch.setattr(ec, "execution_for_store", store_exists)
    gate = SimpleNamespace()
    with pytest.raises(ValueError, match="selected member is unavailable"):
        _GateMixin.resolve_spawn_execution(
            gate,
            parent_session_key="dashboard:old-chat",
            memory_store="private-store",
            _record=None,
            _inherited_selection=("member", "gone"),
        )


def test_prompt_builder_sees_no_member_for_a_pruned_crewmate(config):
    """``member_context_identity`` raised for a member name that is not configured."""
    from kiro_crew.member_essential_context import member_context_identity

    execution = ec.execution_from_record(_record())
    member = execution.member_id or (
        execution.selection_name if execution.selection_kind == "member" else ""
    )
    assert member == ""
    assert member_context_identity(member, member_is_id=False) == ("", "")


def test_run_reader_decodes_through_the_same_seam(config):
    from kiro_crew.subagent_persistence import read_run_execution

    state = dict(_record())
    execution = read_run_execution("run-1", state=state)
    assert execution == replace(execution, selection_kind="template")
    assert execution.selection_kind == "template"


def test_removed_names_come_from_every_prune_marker(tmp_path, monkeypatch):
    from kiro_crew import crewmate_prune_migration as mig

    monkeypatch.setattr(mig, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(mig, "_removed_cache", None)
    assert mig.removed_crewmate_names() == frozenset()
    (tmp_path / "crewmate_prune_v2_migrated.json").write_text('{"removed": ["old"]}')
    (tmp_path / mig.PRUNE_MARKER).write_text('{"removed": ["new", 3, ""], "kept": ["k"]}')
    (tmp_path / "crewmate_prune_migrated.json").write_text("not json")
    assert mig.removed_crewmate_names() == frozenset({"old", "new"})
