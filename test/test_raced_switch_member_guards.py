"""A different agent winning an agent switch's race is recorded only past the member guards.

The owner's choice runs the member guards before it writes the durable selection.
A raced winner (an unlocked writer storing another agent while the session reset
awaits) must pass the same guards before its selection is recorded, and the record
then names the agent the slot holds at commit.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from kiro_crew.config.loader import KiroCrewConfig, ResolvedBindings
from kiro_crew.config.memory_sections import MemoryStoreConfig
from kiro_crew.dashboard import chat_handlers
from kiro_crew.execution_context import (
    ExecutionContext,
    MemoryStoreRef,
    bind_session_execution,
    read_session_execution,
)

KEY = "dashboard:root"
CHOICE = "kirocrew-team-lead"
RACED = "member-x"


def _record_default() -> None:
    bind_session_execution(
        KEY,
        ExecutionContext(
            None, MemoryStoreRef("default"), "template", "kirocrew", selection_name="kirocrew"
        ),
    )


def _recorded() -> str:
    record = read_session_execution(KEY)
    return (record.selection_name or record.template_id) if record else ""


def _env(tmp_path, monkeypatch, *, raced_store: str):
    from chat_test_helpers import _make_state

    from kiro_crew.history import ConversationLog

    mod = "kiro_crew.dashboard.chat_handlers"
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    cfg = KiroCrewConfig()
    cfg.memory_stores["member-store"] = MemoryStoreConfig(owner_member_id="m1", memory_version=2)
    monkeypatch.setattr(f"{mod}.KiroCrewConfig.load", lambda: cfg)

    def bindings(_cfg, name, *_a, **_k):
        store = raced_store if name == RACED else "default"
        return ResolvedBindings(
            workspace_dir=tmp_path / "ws",
            effective_memory_config={},
            memory_store_name=store,
            requested_resolved=True,
            resolved_alias=name,
            kiro_agent=name,
            selection_kind="template",
        )

    monkeypatch.setattr(f"{mod}.resolve_agent_bindings", bindings)
    monkeypatch.setattr(f"{mod}._workspace_name_for_dir", lambda cfg, ws_dir: "default")
    monkeypatch.setattr(f"{mod}.cached_project_agent_names", lambda project_dir: frozenset())
    monkeypatch.setattr(f"{mod}.default_project_dir", lambda ws: str(tmp_path / "ws"))
    monkeypatch.setattr(f"{mod}.warm_project_agent_names", AsyncMock())
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.conversation_log = ConversationLog()
    slot = state.get_or_create_slot("root")
    slot.agent, slot.agent_kind = "kirocrew-conductor", "template"

    async def other_agent_wins(*_a, **_k):
        slot.agent = RACED
        return True

    state.sessions.reset = AsyncMock(side_effect=other_agent_wins)
    _record_default()
    return state, slot


async def _switch(state, slot):
    caller = chat_handlers.SlotAgentSwitchCaller.for_crew_mode_migration()
    return await chat_handlers.switch_slot_agent(state, slot, "root", CHOICE, "template", caller)


@pytest.mark.asyncio
async def test_a_raced_member_never_moves_a_chat_that_has_history(tmp_path, monkeypatch):
    state, slot = _env(tmp_path, monkeypatch, raced_store="member-store")
    slot.append("user", "an earlier turn", "msg msg-u")
    resp = await _switch(state, slot)
    assert resp.status == 200, resp.text
    assert slot.agent == RACED
    assert _recorded() == "kirocrew"


@pytest.mark.asyncio
async def test_a_raced_agent_past_the_guards_is_recorded_as_the_slot_holds_it(
    tmp_path, monkeypatch
):
    state, slot = _env(tmp_path, monkeypatch, raced_store="default")
    resp = await _switch(state, slot)
    assert resp.status == 200, resp.text
    assert slot.agent == RACED
    assert _recorded() == RACED
    assert state.conversation_log.get_metadata(KEY).get("agent") == RACED
