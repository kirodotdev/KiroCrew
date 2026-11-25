"""``includeCrossSessionContext: false`` on an agent spec keeps other sessions' content out.

The spec key is the operator's ``memory.inject_memory`` / ``memory.inject_lessons``
toggles for one agent: it drops the memory and lessons context groups, so the stored
memory block, the learned corrections, the user profile and the recent-session
snippets never reach that agent's sessions, at session start, after a compaction or
on a follow-up message. An agent that does not carry the key is built as before.

Every block is populated, since an "absent" check on an empty store passes with no gate.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew import agent_spec_schema
from kiro_crew import context as ctx_mod
from kiro_crew.config.loader import config_path
from kiro_crew.context import (
    CONTEXT_GROUP_LESSONS,
    CONTEXT_GROUP_MEMORY,
    CONTEXT_GROUP_PROJECT,
    SWITCHABLE_CONTEXT_GROUPS,
    ContextBuilder,
)
from kiro_crew.context_assembly import agent_spec, inclusion
from kiro_crew.learn import Lesson, LessonStore
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader

pytestmark = pytest.mark.usefixtures("close_skills_loaders")

KEY = "dashboard:chat-opt-out"
OPTED_OUT = "scoped-agent"
PLAIN = "plain-agent"

MEMORY = "Prefers tabs over spaces"
LESSON = "Always pass encoding=utf-8"
PROFILE = "[USER PROFILE]"
HISTORY = "Remember the code word ZEBRA42"
REINJECTED_MEMORY = "Call memory_recall with specific keywords"


@pytest.fixture
def write_spec(tmp_path, monkeypatch):
    """Write one agent's spec into an agents directory only this test reads."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    monkeypatch.setattr("kiro_crew.context.kiro_agents_dir", lambda: agents_dir)

    def write(name: str, **fields: Any) -> None:
        (agents_dir / f"{name}.json").write_text(
            json.dumps({"name": name, **fields}), encoding="utf-8"
        )
        ctx_mod.invalidate_include_crew_context_cache()

    return write


@pytest.fixture
def builder(tmp_path) -> ContextBuilder:
    """A builder whose memory, lessons, profile and recent-session blocks all have content."""
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # The onboarding answer puts a [USER PROFILE] block in the lessons group.
    p.write_text(json.dumps({"dashboard": {"user_role": "developer"}}), encoding="utf-8")
    memory = MemoryStore(workspace=tmp_path / "ws")
    memory.write_preferences(f"# User Preferences\n\n- {MEMORY}\n")
    lessons = LessonStore(base_dir=tmp_path)
    lessons.save(Lesson(ts="2026-01-01T00:00:00Z", rule=LESSON, category="tool"))
    built = ContextBuilder(
        memory=memory,
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        lessons=lessons,
    )
    built.conversation_log = SimpleNamespace(
        recent=lambda *args, **kwargs: [],
        read_messages=lambda *args, **kwargs: [],
        recent_with_provenance=lambda *args, **kwargs: [
            {"source_thread": "other-thread", "ts": "2026-09-18T12:00:00", "snippet": HISTORY}
        ],
    )
    return built


def _session_start(builder: ContextBuilder, agent: str, **kwargs: Any) -> str:
    return builder.build_session_context(agent=agent, session_key=KEY, **kwargs)


class TestSessionStart:
    """Each stored block, with the key off and with it absent, true and malformed."""

    @pytest.mark.parametrize("block", [MEMORY, LESSON, PROFILE, HISTORY])
    def test_block_is_withheld_when_the_spec_opts_out(self, builder, write_spec, block):
        write_spec(OPTED_OUT, includeCrossSessionContext=False)
        assert block not in _session_start(builder, OPTED_OUT)

    @pytest.mark.parametrize("block", [MEMORY, LESSON, PROFILE, HISTORY])
    def test_block_is_injected_when_the_spec_has_no_key(self, builder, write_spec, block):
        write_spec(PLAIN)
        assert block in _session_start(builder, PLAIN)

    @pytest.mark.parametrize("block", [MEMORY, LESSON, PROFILE, HISTORY])
    def test_block_is_injected_when_the_key_is_true(self, builder, write_spec, block):
        write_spec(PLAIN, includeCrossSessionContext=True)
        assert block in _session_start(builder, PLAIN)

    @pytest.mark.parametrize("block", [MEMORY, LESSON, PROFILE, HISTORY])
    def test_block_is_injected_for_an_agent_with_no_spec(self, builder, write_spec, block):
        assert block in _session_start(builder, "no-such-agent")

    @pytest.mark.parametrize("value", ["false", "true", 0, 1, None, [], {}])
    def test_a_key_that_is_not_a_boolean_withholds(self, builder, write_spec, value):
        """The key decides what a session may read, so only an exact ``true`` keeps it."""
        write_spec(OPTED_OUT, includeCrossSessionContext=value)
        text = _session_start(builder, OPTED_OUT)
        for block in (MEMORY, LESSON, PROFILE, HISTORY):
            assert block not in text

    def test_the_rest_of_the_context_survives(self, builder, write_spec):
        """Only the stored blocks go: the rules and runtime facts are still sent."""
        write_spec(OPTED_OUT, includeCrossSessionContext=False)
        text = _session_start(builder, OPTED_OUT)
        assert "[CRITICAL RULES" in text
        assert "[CURRENT DATE]" in text
        assert f"[CURRENT AGENT] {OPTED_OUT}" in text

    def test_the_opt_out_is_not_reported_as_a_parents_choice(self, builder, write_spec):
        """[CONTEXT SCOPE] says "your parent withheld", which this is not."""
        write_spec(OPTED_OUT, includeCrossSessionContext=False)
        assert "[CONTEXT SCOPE]" not in _session_start(builder, OPTED_OUT)

    def test_a_parent_granting_every_group_cannot_add_the_blocks_back(self, builder, write_spec):
        write_spec(OPTED_OUT, includeCrossSessionContext=False)
        text = _session_start(
            builder, OPTED_OUT, context_groups=frozenset(SWITCHABLE_CONTEXT_GROUPS)
        )
        for block in (MEMORY, LESSON, PROFILE, HISTORY):
            assert block not in text

    def test_the_built_in_agent_is_never_opted_out(self, builder, write_spec):
        """Its spec is generated, so a key found there is not an author's choice."""
        write_spec("kirocrew", includeCrossSessionContext=False)
        text = _session_start(builder, "kirocrew")
        assert MEMORY in text and LESSON in text


class TestBuildMessage:
    """The turn builder narrows for itself, on a first message and on later ones."""

    def test_first_message_carries_no_stored_block(self, builder, write_spec):
        write_spec(OPTED_OUT, includeCrossSessionContext=False)
        message, _ = builder.build_message("hi", True, KEY, agent=OPTED_OUT)
        for block in (MEMORY, LESSON, PROFILE, HISTORY):
            assert block not in message
        assert "This is your memory" not in message

    def test_first_message_carries_every_stored_block_by_default(self, builder, write_spec):
        write_spec(PLAIN)
        message, _ = builder.build_message("hi", True, KEY, agent=PLAIN)
        for block in (MEMORY, LESSON, PROFILE, HISTORY):
            assert block in message
        assert "This is your memory" in message

    def test_reinjection_after_a_compaction_restores_no_memory_or_lessons(
        self, builder, write_spec
    ):
        write_spec(OPTED_OUT, includeCrossSessionContext=False)
        message, _ = builder.build_message(
            "hi", is_new_session=False, needs_reinjection=True, agent=OPTED_OUT
        )
        assert REINJECTED_MEMORY not in message
        assert LESSON not in message

    def test_reinjection_after_a_compaction_restores_them_by_default(self, builder, write_spec):
        write_spec(PLAIN)
        message, _ = builder.build_message(
            "hi", is_new_session=False, needs_reinjection=True, agent=PLAIN
        )
        assert REINJECTED_MEMORY in message
        assert LESSON in message

    @pytest.mark.parametrize(
        ("agent", "fields", "lessons_in_scope"),
        [(OPTED_OUT, {"includeCrossSessionContext": False}, False), (PLAIN, {}, True)],
    )
    def test_per_message_lessons_get_the_agents_scope(
        self, builder, write_spec, monkeypatch, agent, fields, lessons_in_scope
    ):
        """The follow-up lessons block is gated on the groups it is handed."""
        write_spec(agent, **fields)
        seen: list[frozenset[str] | None] = []

        def record(_text, _session_key, *, context_groups, **_kwargs):
            seen.append(context_groups)
            return ""

        monkeypatch.setattr(builder, "_turn_lessons_block", record)
        builder.build_message("a follow-up", False, KEY, agent=agent)
        assert len(seen) == 1
        assert inclusion._group_included(seen[0], CONTEXT_GROUP_LESSONS) is lessons_in_scope


class TestAgentScopedGroups:
    def test_an_agent_that_did_not_opt_out_keeps_the_callers_scope(self, write_spec):
        write_spec(PLAIN)
        narrowed = frozenset({CONTEXT_GROUP_MEMORY})
        assert inclusion._agent_scoped_groups(PLAIN, None) is None
        assert inclusion._agent_scoped_groups(PLAIN, narrowed) is narrowed
        assert inclusion._agent_scoped_groups(None, None) is None

    def test_an_opted_out_agent_loses_memory_and_lessons_only(self, write_spec):
        write_spec(OPTED_OUT, includeCrossSessionContext=False)
        project = frozenset({CONTEXT_GROUP_PROJECT})
        assert inclusion._agent_scoped_groups(OPTED_OUT, None) == project
        assert (
            inclusion._agent_scoped_groups(OPTED_OUT, frozenset(SWITCHABLE_CONTEXT_GROUPS))
            == project
        )

    def test_it_never_adds_a_group_the_caller_withheld(self, write_spec):
        write_spec(OPTED_OUT, includeCrossSessionContext=False)
        assert inclusion._agent_scoped_groups(OPTED_OUT, frozenset()) == frozenset()
        assert inclusion._agent_scoped_groups(OPTED_OUT, frozenset({CONTEXT_GROUP_LESSONS})) == (
            frozenset()
        )


class TestSpecRead:
    def test_an_edited_spec_is_read_again_after_the_agents_are_rescanned(
        self, tmp_path, monkeypatch
    ):
        """The answer is kept per agent; a rescan of the agents directory drops it."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        monkeypatch.setattr("kiro_crew.context.kiro_agents_dir", lambda: agents_dir)
        spec = agents_dir / f"{OPTED_OUT}.json"
        spec.write_text(json.dumps({"name": OPTED_OUT}), encoding="utf-8")
        assert inclusion._agent_includes_cross_session_context(OPTED_OUT) is True

        spec.write_text(
            json.dumps({"name": OPTED_OUT, "includeCrossSessionContext": False}),
            encoding="utf-8",
        )
        assert inclusion._agent_includes_cross_session_context(OPTED_OUT) is True
        ctx_mod.invalidate_include_crew_context_cache()
        assert inclusion._agent_includes_cross_session_context(OPTED_OUT) is False

    def test_the_two_spec_flags_are_read_apart(self, write_spec):
        write_spec(OPTED_OUT, includeCrossSessionContext=False)
        assert agent_spec._read_include_cross_session_context(OPTED_OUT) is False
        assert agent_spec._read_include_crew_context(OPTED_OUT) is True
        write_spec(PLAIN, includeCrewContext=False)
        assert agent_spec._read_include_cross_session_context(PLAIN) is True
        assert agent_spec._read_include_crew_context(PLAIN) is False

    def test_the_spec_checker_knows_the_key(self):
        assert agent_spec_schema.check_spec({"includeCrossSessionContext": False}) == []
        assert dict(agent_spec_schema.check_spec({"includeCrossSessionContext": "no"})) == {
            "includeCrossSessionContext": "must be true or false"
        }
