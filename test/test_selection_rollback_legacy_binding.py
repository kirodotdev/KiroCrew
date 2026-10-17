"""A selection rollback puts a pre-execution-record line's own binding back.

A transcript written before the execution record existed binds its memory through
the metadata line's ``memory_store`` / ``memory_mode`` fields alone. A selection
write overwrites both, so rolling that write back must restore the line's own
values rather than delete them, or the record routes differently on its next turn.

The rollback is exercised on the durable path: a persistent publication leaves no
live carrier, which is the path that rewrites the metadata line.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew import execution_context as execution
from kiro_crew.config.sections import MemoryStoreConfig
from kiro_crew.history import ConversationLog
from kiro_crew.session_agent_selection import (
    SelectionChange,
    record_agent_selection,
    restore_agent_selection,
)

TEMPLATE = "kirocrew-worker"


@pytest.fixture
def legacy_store(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    from kiro_crew.config import loader

    loader._invalidate_config_cache()
    execution._LIVE_EXECUTIONS.clear()
    execution._VOUCHED_EXECUTIONS.clear()
    cfg = SimpleNamespace(
        agents={}, memory_stores={"legacy-v1": MemoryStoreConfig(memory_version=1)}
    )
    monkeypatch.setattr(loader.KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    yield cfg
    execution._LIVE_EXECUTIONS.clear()
    execution._VOUCHED_EXECUTIONS.clear()


def _template_bindings():
    return SimpleNamespace(
        selection_kind="template",
        resolved_alias=TEMPLATE,
        requested_resolved=True,
        kiro_agent=TEMPLATE,
        memory_store_name="default",
        selection_revision="",
        execution_context=None,
    )


def _publish(key):
    change = record_agent_selection(key, TEMPLATE, _template_bindings())
    assert change is not None
    # Precondition: the write really replaced the line's binding, so a pass below
    # means the rollback restored it rather than that nothing was written.
    written = ConversationLog().get_metadata(key)
    assert written[execution.EXECUTION_CONTEXT_KEY] == change[1]
    assert written.get("memory_store") != "legacy-v1"
    assert execution.read_live_session_execution(key) is None
    return change


@pytest.mark.parametrize(
    "line",
    [
        {"agent": "kirocrew", "memory_store": "legacy-v1", "memory_mode": "persistent"},
        {"agent": "kirocrew", "memory_store": "legacy-v1"},
        {"agent": "kirocrew", "memory_mode": "persistent"},
    ],
    ids=["store-and-mode", "store-only", "mode-only"],
)
def test_rollback_restores_the_lines_own_binding(legacy_store, line):
    key = "dashboard_legacy_binding"
    log = ConversationLog()
    log.update_metadata(key, line)
    before = log.get_metadata(key)
    assert execution.read_session_execution(key) is None

    change = _publish(key)
    assert change[0] is None

    restore_agent_selection(key, change)

    after = ConversationLog().get_metadata(key)
    assert after == before
    assert execution.read_session_execution(key) is None


def test_rollback_of_a_line_with_no_binding_leaves_none(legacy_store):
    key = "dashboard_unbound_line"
    log = ConversationLog()
    log.update_metadata(key, {"agent": "kirocrew"})
    before = log.get_metadata(key)

    restore_agent_selection(key, _publish(key))

    after = ConversationLog().get_metadata(key)
    assert after == before
    assert "memory_store" not in after
    assert "memory_mode" not in after
    assert execution.EXECUTION_CONTEXT_KEY not in after


def test_selection_change_keeps_its_positional_shape():
    change = SelectionChange(None, {"store": {"store_id": "default"}})
    assert change[0] is None
    assert change[1] == {"store": {"store_id": "default"}}
    assert change.legacy is None
