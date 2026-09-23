"""A session written before ``execution_context`` existed can still be saved and closed.

Such a record names a member's V2 store in ``memory_store`` but carries no
canonical execution identity. Reading it FOR private memory is refused, and stays
refused. Writing its own transcript is not private-memory access: the save only
asks the carrier for a retention mode, and with no carrier the slot's own mode is
the only retention there is. Before this, every save and every close of such a
session raised, the close handler restored the slot, and the tab could never be
dismissed.
"""

from __future__ import annotations

import pytest
from chat_test_helpers import _make_state

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.dashboard import chat_handlers as handlers
from kiro_crew.execution_context import read_session_execution
from kiro_crew.history import ConversationLog
from kiro_crew.memory_stores import (
    MissingExecutionIdentity,
    UnknownMemoryStore,
    provision_member_memory,
)

pytestmark = pytest.mark.usefixtures("healthy_host_memory")

NAME = "legacy-member-chat"
KEY = f"dashboard:{NAME}"


@pytest.fixture
def legacy_store() -> str:
    cfg = KiroCrewConfig.load()
    cfg.agents["writer"] = KiroCrewAgentConfig(kiro_agent="kirocrew", triggers="write")
    store = provision_member_memory(cfg, "writer")
    cfg.save()
    return store


def _state_with_legacy_slot(tmp_path, legacy_store: str, *, memory_mode: str = "persistent"):
    state = _make_state(tmp_path)
    # One log for both readers: ``read_session_execution`` opens the default
    # ``ConversationLog()``; the save writes through ``state.conversation_log``.
    state.conversation_log = ConversationLog()
    state.conversation_log.update_metadata(
        KEY, {"agent": "writer", "memory_store": legacy_store, "title": "Old chat"}
    )
    slot = state.get_or_create_slot(NAME)
    # What rehydration mirrors from that record onto the live slot.
    slot.agent = "writer"
    slot.memory_store = legacy_store
    slot.memory_mode = memory_mode
    slot.append("user", "written on the old code")
    slot.append("assistant", "before execution_context existed")
    slot.drain()
    return state, slot


def _legacy_identity_is_still_refused() -> None:
    # The same non-required read the save path makes: a record naming a V2
    # store with no carrier is refused as identity, not treated as Global.
    with pytest.raises(UnknownMemoryStore, match="canonical member identity"):
        read_session_execution(KEY)


@pytest.mark.asyncio
async def test_legacy_v2_record_saves_its_transcript(tmp_path, legacy_store):
    state, slot = _state_with_legacy_slot(tmp_path, legacy_store)
    _legacy_identity_is_still_refused()

    assert await handlers.save_slot_off_loop(state, slot, best_effort=False)

    contents = [m.get("content") for m in state.conversation_log.read_messages(KEY)]
    assert contents == ["written on the old code", "before execution_context existed"]
    # The save granted nothing: the record still has no canonical identity.
    _legacy_identity_is_still_refused()


@pytest.mark.asyncio
async def test_legacy_v2_record_close_archives_and_does_not_restore(tmp_path, legacy_store):
    state, slot = _state_with_legacy_slot(tmp_path, legacy_store)

    await handlers._close_slot(state, slot, NAME)

    assert NAME not in state._slots, "a failed archive restored the slot to the list"
    meta = state.conversation_log.get_metadata(KEY)
    assert meta.get("closed") is True
    _legacy_identity_is_still_refused()


@pytest.mark.asyncio
async def test_legacy_v2_record_in_a_restricted_slot_still_writes_nothing(tmp_path, legacy_store):
    state, slot = _state_with_legacy_slot(tmp_path, legacy_store, memory_mode="temporary")

    assert await handlers.save_slot_off_loop(state, slot, best_effort=False)

    assert state.conversation_log.read_messages(KEY) == []


@pytest.mark.asyncio
async def test_malformed_carrier_is_not_the_legacy_case(tmp_path, legacy_store):
    """A record that HAS ``execution_context`` but cannot decode it keeps refusing.

    The fallback is for a field that never existed, not for one that is broken:
    a malformed carrier's retention is unknown, so the save must not guess.
    """
    state, slot = _state_with_legacy_slot(tmp_path, legacy_store)
    state.conversation_log.update_metadata(KEY, {"execution_context": {"store": "garbage"}})

    with pytest.raises(UnknownMemoryStore, match="malformed execution context"):
        await handlers.save_slot_off_loop(state, slot, best_effort=False)

    assert state.conversation_log.read_messages(KEY) == []


@pytest.mark.asyncio
async def test_undeclared_store_is_not_the_legacy_case(tmp_path, legacy_store):
    """No carrier AND a store nobody declares: retention is unknown, refusal stands."""
    state, slot = _state_with_legacy_slot(tmp_path, legacy_store)
    state.conversation_log.update_metadata(KEY, {"memory_store": "member-nobody-0123"})
    slot.memory_store = "member-nobody-0123"

    with pytest.raises(UnknownMemoryStore, match="declaration is unavailable"):
        await handlers.save_slot_off_loop(state, slot, best_effort=False)

    assert state.conversation_log.read_messages(KEY) == []


def test_legacy_refusal_is_the_dedicated_type(legacy_store):
    """The save keys on the TYPE, so the two absent-carrier sites must raise it."""
    log = ConversationLog()
    log.update_metadata("dashboard:by-store", {"memory_store": legacy_store})
    log.update_metadata("dashboard:by-marker", {"member_id": "writer"})
    for key in ("dashboard:by-store", "dashboard:by-marker"):
        with pytest.raises(MissingExecutionIdentity, match="canonical member identity"):
            read_session_execution(key)
