"""A member session written before ``execution_context`` existed is readable again.

0.7.0.5 wrote a member chat's binding as ``{agent, memory_store}``; 0.7.0.6 made
``execution_context`` the canonical carrier, and the start-of-process store
migration backfills the config and the V2 store but not the session record, so
without the read-time backfill ``read_session_execution`` refuses every such chat.

The read derives the carrier from the store's declared owner and persists it,
ONLY when the attribution is unambiguous: the store is a declared V2 store whose
``owner_member_id`` names exactly one configured member, that member is bound to
this store, and the record's own ``agent`` names that member. Every other shape
keeps refusing -- no member is ever guessed -- and the refusal names the remedy.
Records that already carry ``execution_context``, Global sessions and V1
named-store sessions are not touched.
"""

from __future__ import annotations

import pytest
from test_member_memory_upgrade import STORE, _write_legacy_home

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.execution_context import (
    EXECUTION_CONTEXT_KEY,
    bind_session_execution,
    capture_session_execution,
    execution_from_record,
    read_session_execution,
    read_vouched_session_execution,
    resolve_member_execution,
)
from kiro_crew.history import ConversationLog
from kiro_crew.memory_stores import (
    LEGACY_MEMBER_STORE_REMEDY,
    MissingExecutionIdentity,
    UnknownMemoryStore,
    repair_legacy_member_stores,
)

KEY = "dashboard:chat-old-member-session"
REMEDY = "open a new chat with the same member"


def _upgraded_home(*bound: str) -> KiroCrewConfig:
    """A 0.7.0.x home after the 0.7.0.8 start-of-process store migration."""
    _write_legacy_home(*bound)
    repair_legacy_member_stores()
    return KiroCrewConfig.load()


def _legacy_record(key: str = KEY, **fields: object) -> dict:
    """What 0.7.0.5 wrote for a member chat: no ``execution_context`` field at all."""
    record = {"agent": "reviewer", "memory_store": STORE, "title": "old member chat"}
    record.update(fields)
    ConversationLog().update_metadata(key, record)
    stored, readable = ConversationLog().get_metadata_status(key)
    assert readable and EXECUTION_CONTEXT_KEY not in stored
    return stored


def _stored(key: str = KEY) -> dict:
    record, readable = ConversationLog().get_metadata_status(key)
    assert readable
    return record


class TestTheUnambiguousRecordIsBackfilled:
    def test_the_read_derives_the_owner_and_persists_the_carrier(self) -> None:
        cfg = _upgraded_home("reviewer")
        assert cfg.memory_stores[STORE].owner_member_id == cfg.agents["reviewer"].member_id
        _legacy_record()

        derived = read_session_execution(KEY)

        expected = resolve_member_execution(cfg, "reviewer")
        assert derived == expected
        assert derived is not None and derived.selection_kind == "member"
        assert derived.store.store_id == STORE
        # One-shot migration: the record now carries the canonical field, so the
        # next read decodes it instead of deriving anything.
        stored = _stored()
        assert execution_from_record(stored) == derived
        assert stored["memory_store"] == STORE
        assert stored["memory_mode"] == "persistent"
        assert stored["agent"] == "reviewer" and stored["title"] == "old member chat"
        assert read_session_execution(KEY) == derived
        assert read_session_execution(KEY, required=True) == derived

    def test_capture_returns_the_same_identity(self) -> None:
        _upgraded_home("reviewer")
        _legacy_record()
        assert capture_session_execution(KEY) == read_session_execution(KEY)

    def test_the_backfill_never_vouches(self) -> None:
        """The store came from the session's own record, so this process cannot
        vouch for it -- the same footing a restarted member session is on."""
        _upgraded_home("reviewer")
        _legacy_record()
        assert read_session_execution(KEY) is not None
        assert read_vouched_session_execution(KEY) is None

    def test_the_member_id_is_also_accepted_as_the_agent(self) -> None:
        cfg = _upgraded_home("reviewer")
        _legacy_record(agent=cfg.agents["reviewer"].member_id)
        derived = read_session_execution(KEY)
        assert derived is not None and derived.member_id == cfg.agents["reviewer"].member_id

    def test_an_app_owned_record_keeps_its_app_attribution(self) -> None:
        """`/api/spawn` reads app identity only from the carrier, and an empty app
        skips the per-app profile half of governance -- so the record's own
        ``app`` must travel into the derived carrier, not be dropped."""
        cfg = _upgraded_home("reviewer")
        _legacy_record(app="auto-improvement")
        derived = read_session_execution(KEY)
        assert derived == resolve_member_execution(cfg, "reviewer", app="auto-improvement")
        assert derived is not None and derived.app == "auto-improvement"
        assert execution_from_record(_stored()).app == "auto-improvement"

    def test_a_record_without_an_app_derives_an_empty_app(self) -> None:
        _upgraded_home("reviewer")
        _legacy_record()
        assert "app" not in _stored()
        derived = read_session_execution(KEY)
        assert derived is not None and derived.app == ""

    def test_a_lost_compare_and_set_rereads_the_winner(self, monkeypatch) -> None:
        """Two first reads race: the loser returns what the winner committed."""
        cfg = _upgraded_home("reviewer")
        _legacy_record()
        winner = resolve_member_execution(cfg, "reviewer")
        real = ConversationLog.update_metadata_if
        calls: list[str] = []

        def racing(self, key, fields, guard, **kwargs):
            calls.append(key)
            # The other reader committed between our read and our write.
            ConversationLog().update_metadata(
                key, {EXECUTION_CONTEXT_KEY: winner.to_record(), "memory_mode": "persistent"}
            )
            return real(self, key, fields, guard, **kwargs)

        monkeypatch.setattr(ConversationLog, "update_metadata_if", racing)
        assert read_session_execution(KEY) == winner
        assert calls == [KEY]
        assert execution_from_record(_stored()) == winner


class TestEveryAmbiguousShapeStaysRefused:
    """No member is ever guessed. The refusal is the pre-existing type, and its
    text now tells the user what to do; the record is left exactly as written."""

    def _refused(self, key: str = KEY, remedy: str = REMEDY) -> None:
        before = _stored(key)
        with pytest.raises(MissingExecutionIdentity, match="canonical member identity") as info:
            read_session_execution(key)
        assert remedy in str(info.value)
        assert "Global was not used" in str(info.value)
        assert _stored(key) == before, "a refused read must not write"

    def test_the_agent_names_a_member_that_does_not_own_the_store(self) -> None:
        _upgraded_home("reviewer")
        _legacy_record(agent="peer")
        self._refused()

    def test_the_agent_names_nobody_configured(self) -> None:
        _upgraded_home("reviewer")
        _legacy_record(agent="departed")
        self._refused()

    def test_a_record_with_no_agent_at_all(self) -> None:
        _upgraded_home("reviewer")
        _legacy_record(agent=None)
        record = _stored()
        assert not record.get("agent")
        self._refused()

    def test_a_template_pick_is_not_a_member_session(self) -> None:
        _upgraded_home("reviewer")
        _legacy_record(agent_kind="template")
        self._refused()

    def test_a_store_the_migration_could_not_attribute(self) -> None:
        """Two members bound to one store: the start-of-process migration skips
        it, so the store has no owner and the session cannot be derived. A new
        chat with the member would fail the same way, so the remedy named is the
        store repair the boot log and ``kirocrew doctor`` use, not a new chat."""
        cfg = _upgraded_home("reviewer", "second")
        assert not cfg.memory_stores[STORE].owner_member_id
        _legacy_record()
        self._refused(remedy=LEGACY_MEMBER_STORE_REMEDY)
        with pytest.raises(MissingExecutionIdentity) as info:
            read_session_execution(KEY)
        assert REMEDY not in str(info.value)

    def test_a_member_marker_without_a_carrier(self) -> None:
        _upgraded_home("reviewer")
        _legacy_record(member_id="reviewer")
        self._refused()

    def test_a_restricted_record_is_not_backfilled(self) -> None:
        """A durable carrier is only ever written for a persistent session."""
        _upgraded_home("reviewer")
        _legacy_record(memory_mode="incognito")
        self._refused()

    def test_a_malformed_app_attribution_is_not_backfilled(self) -> None:
        """An ``app`` that is not a string cannot be carried, so nothing is derived."""
        _upgraded_home("reviewer")
        _legacy_record(app={"id": "auto-improvement"})
        self._refused()


class TestEveryOtherPathIsUnchanged:
    def test_a_record_that_already_has_a_carrier_is_read_not_rewritten(self) -> None:
        cfg = _upgraded_home("reviewer")
        bound = resolve_member_execution(cfg, "reviewer")
        bind_session_execution(KEY, bound)
        before = _stored()
        assert read_session_execution(KEY) == bound
        assert _stored() == before

    def test_a_global_session_reads_as_none(self) -> None:
        _upgraded_home("reviewer")
        _legacy_record(memory_store=None, agent="peer")
        before = _stored()
        assert read_session_execution(KEY) is None
        assert _stored() == before

    def test_a_v1_named_store_session_reads_as_none(self) -> None:
        cfg = _upgraded_home("reviewer")
        cfg.memory_stores["shared"] = type(cfg.memory_stores["default"])()
        cfg.save()
        _legacy_record(agent="peer", memory_store="shared")
        before = _stored()
        assert read_session_execution(KEY) is None
        assert _stored() == before

    def test_an_undeclared_store_is_a_plain_unknown_store(self) -> None:
        _upgraded_home("reviewer")
        _legacy_record(memory_store="member-gone-0000")
        before = _stored()
        with pytest.raises(UnknownMemoryStore) as info:
            read_session_execution(KEY)
        assert not isinstance(info.value, MissingExecutionIdentity)
        assert _stored() == before

    def test_a_malformed_carrier_is_not_the_legacy_case(self) -> None:
        _upgraded_home("reviewer")
        _legacy_record()
        ConversationLog().update_metadata(KEY, {EXECUTION_CONTEXT_KEY: {"store": "garbage"}})
        before = _stored()
        with pytest.raises(UnknownMemoryStore, match="malformed execution context") as info:
            read_session_execution(KEY)
        assert not isinstance(info.value, MissingExecutionIdentity)
        assert _stored() == before
