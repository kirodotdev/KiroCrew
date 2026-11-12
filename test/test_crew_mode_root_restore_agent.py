"""A restored agent line that disagrees with the durable selection, and the save after it."""

from __future__ import annotations

import logging

from kiro_crew.dashboard.slot_persistence.restore_inputs import _restored_agent_name
from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef, bind_session_execution

KEY = "dashboard:crew-root"
LEAD = "kirocrew-team-lead"


def _default_record(key: str, *, replace: bool = False) -> None:
    bind_session_execution(
        key,
        ExecutionContext(
            None, MemoryStoreRef("default"), "template", "kirocrew", selection_name="kirocrew"
        ),
        replace_existing=replace,
    )


def test_a_disagreeing_line_restores_the_record_and_warns(tmp_path, monkeypatch, caplog):
    # The record is canonical; the line is editable by the session itself.
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    _default_record(KEY)
    meta = {"agent": LEAD, "agent_kind": "template", "crew_mode_agent": LEAD}
    with caplog.at_level(logging.WARNING):
        assert _restored_agent_name(KEY, meta) == "kirocrew"
    warned = [r.getMessage() for r in caplog.records if "disagrees" in r.getMessage()]
    assert len(warned) == 1
    assert KEY in warned[0] and repr(LEAD) in warned[0] and "'kirocrew'" in warned[0]


def test_an_agreeing_line_restores_quietly(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    _default_record(KEY)
    with caplog.at_level(logging.WARNING):
        assert _restored_agent_name(KEY, {"agent": "kirocrew"}) == "kirocrew"
    assert "disagrees" not in caplog.text


def test_a_save_that_keeps_foreign_rows_keeps_the_agent_binding(tmp_path, monkeypatch, caplog):
    """A slot built on the key without the line saves a short window: the rows on
    disk are kept as another writer's, and the line must keep the agent fields."""
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
    from kiro_crew.history import ConversationLog

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.conversation_log = ConversationLog()
    root = state.get_or_create_slot("root")
    root.agent, root.agent_kind, root.crew_mode_agent = LEAD, "template", LEAD
    for i in range(3):
        root.append("user", f"go {i}", "msg msg-u")
    assert _save_slot_to_history(state, root)
    _default_record("dashboard:root", replace=True)
    state._slots.pop("root")

    fresh = state.get_or_create_slot("root")
    assert fresh.agent == ""
    fresh.append("user", "hi", "msg msg-u")
    with caplog.at_level(logging.WARNING):
        assert _save_slot_to_history(state, fresh)
    assert "another writer appended" in caplog.text
    meta = state.conversation_log.get_metadata("dashboard:root")
    assert (meta.get("agent"), meta.get("agent_kind"), meta.get("crew_mode_agent")) == (
        LEAD,
        "template",
        LEAD,
    )
    assert meta.get("execution_context", {}).get("selection_name") == "kirocrew"
