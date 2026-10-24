"""A fork carrying a crewmate alias in ``template_id`` is repaired on read.

A fork of a carrier-less chat can record the parent's agent NAME where the
carrier holds a kiro-cli template name, so a chat on the ``default`` crewmate
lands as ``{"selection_kind": "template", "template_id": "default"}``.
``resolve_session_agent_bindings`` launches ``template_id`` verbatim, no agent
called ``default`` exists, and the child cannot run a turn. Such a child stays
frozen because ``read_session_execution`` answers from any carrier that
decodes, so nothing looks at it again.

The read rebinds exactly that shape to the crewmate the name resolves to, on
the same ``_resolve_agent_selection`` rule the fork path and the parent's own
turns apply, and persists it once. Nothing is guessed: a deliberate template
session, an ordinary template carrier, a name that is no alias, a carrier whose
two names disagree, a restricted chat and a V2 crewmate are all left exactly as
they are.
"""

from __future__ import annotations

import pytest

from kiro_crew import memory_stores
from kiro_crew.config.loader import (
    KiroCrewAgentConfig,
    KiroCrewConfig,
    resolve_agent_bindings,
)
from kiro_crew.execution_context import (
    EXECUTION_CONTEXT_KEY,
    execution_from_record,
    read_session_execution,
    resolve_member_execution,
)
from kiro_crew.history import ConversationLog
from kiro_crew.session_agent_selection import resolve_session_agent_bindings
from kiro_crew.config import loader_snapshots

KEY = "dashboard:chat-pre-fix-fork"
PARENT_KEY = "dashboard:chat-legacy-parent"


def _crew() -> KiroCrewConfig:
    """A home whose ``default`` alias binds the ``kirocrew`` template."""
    cfg = KiroCrewConfig.load()
    cfg.agents["default"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    cfg.agents["builder"] = KiroCrewAgentConfig(kiro_agent="amzn-builder", model="sonnet")
    cfg.save()
    return KiroCrewConfig.load()


def _carrier(**over: object) -> dict:
    """The carrier the pre-fix fork wrote for a parent on the ``default`` alias."""
    carrier = {
        "member_id": None,
        "store": {"store_id": "default", "member_id": None},
        "selection_kind": "template",
        "template_id": "default",
        "memory_mode": "persistent",
        "app": "",
        "selection_name": "",
        "selection_revision": "",
    }
    carrier.update(over)
    return carrier


def _record(
    key: str = KEY,
    *,
    agent: str = "default",
    parent_fields: dict | None = None,
    **fields: object,
) -> dict:
    parent = {
        "agent": agent,
        "memory_mode": "persistent",
        "title": "old conversation",
    }
    parent.update(parent_fields or {})
    ConversationLog().update_metadata(PARENT_KEY, parent)
    record: dict = {
        "agent": agent,
        "memory_mode": "persistent",
        "title": "\u21b3 Fork of an old conversation",
        "forked_from": PARENT_KEY,
        EXECUTION_CONTEXT_KEY: _carrier(),
    }
    record.update(fields)
    ConversationLog().update_metadata(key, record)
    return _stored(key)


def _stored(key: str = KEY) -> dict:
    record, readable = ConversationLog().get_metadata_status(key)
    assert readable
    return record


class TestThePreFixForkIsRebound:
    def test_the_read_rebinds_the_alias_and_persists_it(self) -> None:
        cfg = _crew()
        _record()

        repaired = read_session_execution(KEY)

        assert repaired == resolve_member_execution(cfg, "default")
        assert repaired is not None
        assert (repaired.selection_kind, repaired.template_id) == ("member", "kirocrew")
        assert repaired.selection_name == "default"
        assert repaired.store.store_id == "default"
        # One-shot: the record now carries the rebound carrier, so the next read
        # decodes it instead of repairing anything.
        stored = _stored()
        assert execution_from_record(stored) == repaired
        assert read_session_execution(KEY) == repaired
        assert read_session_execution(KEY, required=True) == repaired
        # Nothing but the carrier is rewritten.
        assert stored["agent"] == "default"
        assert stored["title"] == "\u21b3 Fork of an old conversation"
        assert stored["forked_from"] == PARENT_KEY

    def test_the_rebound_chat_can_run_a_turn_again(self) -> None:
        """The end the user feels: the chat resolves an agent instead of refusing.

        Before the repair the stored ``template_id`` is launched verbatim, so
        the turn asks for an agent called ``default`` that does not exist.
        """
        cfg = _crew()
        _record()

        read_session_execution(KEY)

        bindings = resolve_session_agent_bindings(resolve_agent_bindings, cfg, KEY, "default")
        assert bindings.requested_resolved
        assert (bindings.kiro_agent, bindings.resolved_alias) == ("kirocrew", "default")

    def test_a_pinned_crewmate_keeps_its_model(self) -> None:
        """The rebind is the crewmate, so its template AND its pin come back."""
        cfg = _crew()
        _record(agent="builder", **{EXECUTION_CONTEXT_KEY: _carrier(template_id="builder")})

        repaired = read_session_execution(KEY)

        assert repaired == resolve_member_execution(cfg, "builder")
        assert repaired is not None and repaired.template_id == "amzn-builder"
        bindings = resolve_session_agent_bindings(resolve_agent_bindings, cfg, KEY, "builder")
        assert bindings.model == "sonnet"


class TestEveryOtherShapeIsLeftAlone:
    @pytest.mark.parametrize(
        "record_fields",
        [
            # A deliberate template pick: the slot was stamped, so the carrier
            # is the truth and the matching alias name is a coincidence.
            pytest.param(
                {"parent_fields": {"agent_kind": "template"}},
                id="template-stamped-parent",
            ),
            pytest.param(
                {"forked_from": "dashboard:chat-missing-parent"},
                id="parent-is-not-readable",
            ),
            pytest.param(
                {"parent_fields": {"agent": "builder"}},
                id="parent-and-child-disagree",
            ),
            pytest.param(
                {"parent_fields": {EXECUTION_CONTEXT_KEY: _carrier()}},
                id="parent-already-has-a-carrier",
            ),
            # An ordinary template carrier records the name in both fields.
            pytest.param(
                {EXECUTION_CONTEXT_KEY: _carrier(selection_name="default")},
                id="ordinary-template-carrier",
            ),
            # The fork copied one field into the other; two different names are
            # some other history and are never reconciled by guessing.
            pytest.param(
                {"agent": "builder", EXECUTION_CONTEXT_KEY: _carrier(template_id="default")},
                id="agent-and-template-disagree",
            ),
            # Not an alias at all: a template, possibly one only a project declares.
            pytest.param(
                {
                    "agent": "project-only-template",
                    EXECUTION_CONTEXT_KEY: _carrier(template_id="project-only-template"),
                },
                id="name-is-not-an-alias",
            ),
            # Deriving a member binding is what opens a store, so a restricted
            # chat is not rewritten by a read.
            pytest.param(
                {
                    "memory_mode": "incognito",
                    EXECUTION_CONTEXT_KEY: _carrier(memory_mode="incognito"),
                },
                id="restricted-chat",
            ),
            pytest.param({"agent": ""}, id="no-agent-recorded"),
        ],
    )
    def test_the_carrier_is_returned_as_stored(self, record_fields: dict) -> None:
        _crew()
        before = _record(**record_fields)

        kept = read_session_execution(KEY)

        assert kept == execution_from_record(before)
        assert kept is not None and kept.selection_kind == "template"
        assert _stored()[EXECUTION_CONTEXT_KEY] == before[EXECUTION_CONTEXT_KEY]

    def test_a_v2_crewmates_private_memory_is_not_opened_by_a_read(self) -> None:
        """A read that only INFERRED the identity never reaches private memory.

        This is the refusal the fork path takes for the same parent, not an
        accident of which store the carrier happens to name.
        """
        cfg = KiroCrewConfig.load()
        cfg.agents["writer"] = KiroCrewAgentConfig()
        store = memory_stores.provision_member_memory(cfg, "writer")
        cfg.save()
        assert KiroCrewConfig.load().memory_stores[store].memory_version == 2
        before = _record(agent="writer", **{EXECUTION_CONTEXT_KEY: _carrier(template_id="writer")})

        kept = read_session_execution(KEY)

        assert kept == execution_from_record(before)
        assert kept is not None and kept.store.store_id == "default"
        assert kept.member_id is None
        assert _stored()[EXECUTION_CONTEXT_KEY] == before[EXECUTION_CONTEXT_KEY]


class TestAnAliasThatIsAlsoATemplateIsLeftAlone:
    def test_a_name_that_still_names_a_materialized_template_is_not_rebound(self) -> None:
        """A legitimate template fork writes the same shape as the broken one.

        When the recorded name resolves to a materialized kiro-cli template, a
        template fork of it produces an identical carrier and an identical
        unstamped parent. The alias lookup alone cannot prove the original
        selection was the member, so the carrier is left as stored and the
        template chat keeps running its template.
        """
        cfg = KiroCrewConfig.load()
        cfg.agents["reporter"] = KiroCrewAgentConfig(kiro_agent="reporter")
        cfg.save()
        loader_snapshots._MATERIALIZED_AGENTS = frozenset({"reporter"})
        try:
            before = _record(
                agent="reporter", **{EXECUTION_CONTEXT_KEY: _carrier(template_id="reporter")}
            )

            kept = read_session_execution(KEY)

            assert kept == execution_from_record(before)
            assert kept is not None and kept.selection_kind == "template"
            assert _stored()[EXECUTION_CONTEXT_KEY] == before[EXECUTION_CONTEXT_KEY]
        finally:
            loader_snapshots._MATERIALIZED_AGENTS = frozenset()

    def test_default_is_still_rebound_because_no_template_shadows_it(self) -> None:
        """The main case is unaffected: no materialized template shadows it."""
        cfg = _crew()
        _record()

        repaired = read_session_execution(KEY)

        assert repaired == resolve_member_execution(cfg, "default")
        assert repaired is not None
        assert (repaired.selection_kind, repaired.template_id) == ("member", "kirocrew")
