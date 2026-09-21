"""Both memory versions leave history and fragment retrieval to the recall tool."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_member_essential_context import env as _member_env

from kiro_crew.context import CONTEXT_GROUP_LESSONS, ContextBuilder
from kiro_crew.learn import LessonStore
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader

env = _member_env


def test_v2_prompt_lifecycles_leave_retrieval_to_the_tool(env, monkeypatch):
    memory = env.memory
    memory.write_preferences("Use a concise reply.")
    memory.write_projects("The active project is Beacon.")
    forbidden = Mock(side_effect=AssertionError("prompt construction attempted retrieval"))
    rules = Mock(return_value="[Scoped correction: run the project checks.]")
    for name in ("recall", "get_semantic_context", "get_episodic_context"):
        monkeypatch.setattr(memory.vector_store, name, forbidden)
    monkeypatch.setattr(memory.vector_store, "get_lessons_context", rules)
    memory.read_recent_history = forbidden
    builder = env.builder
    binding = dict(member=env.member, memory_store=env.store, project=str(env.project))
    first, _ = builder.build_message(
        "Find our earlier deployment decision", True, "session", **binding
    )
    assert "Use a concise reply." in first
    assert "The active project is Beacon." in first
    assert "Scoped correction" in first and "memory_recall" in first
    for options in ({}, {"needs_reinjection": True}):
        builder.build_message("Now another topic", False, "session", **binding, **options)
    builder.build_message("Continue after restart", True, "session", resumed=True, **binding)
    forbidden.assert_not_called()
    assert rules.call_args_list
    # The member lessons renderer must rank against the real request, matching
    # its sibling vector renderer and the get_lessons_context contract, which
    # forbids an empty query as filler in background admission. Every call
    # therefore carries the text of the message that produced it, never "".
    issued = {
        "Find our earlier deployment decision",
        "Now another topic",
        "Continue after restart",
    }
    assert all(call.kwargs["query_text"] in issued for call in rules.call_args_list)
    assert all(call.kwargs["query_text"] for call in rules.call_args_list)


def test_v1_new_session_keeps_preferences_and_defers_history_to_recall(tmp_path):
    memory = MemoryStore(workspace=tmp_path / "workspace")
    memory.write_preferences("Use a concise reply.")
    memory.write_projects("The active project is Beacon.")
    forbidden = Mock(side_effect=AssertionError("prompt construction attempted eager retrieval"))
    memory.read_recent_history = forbidden
    preferences = Mock(return_value="[Semantic Memory]\nPreference fact sentinel")
    lessons = Mock(return_value="[Lessons Learned]\nRelevant correction sentinel")
    memory._vector_store = SimpleNamespace(
        algorithm_version="v1",
        get_semantic_context=forbidden,
        get_episodic_context=forbidden,
        get_preferences_context=preferences,
        has_any_lesson=lambda: True,
        get_lessons_context=lessons,
    )
    builder = ContextBuilder(
        memory=memory,
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        lessons=LessonStore(base_dir=tmp_path),
    )
    query = "Find our earlier deployment decision"
    first, _ = builder.build_message(query, True, "session")
    for sentinel in (
        "Use a concise reply.",
        "Preference fact sentinel",
        "Relevant correction sentinel",
        "memory_recall",
        "[End of memory activity index]",
    ):
        assert sentinel in first
    forbidden.assert_not_called()
    preferences.assert_called_once()
    lessons.assert_called_once()

    builder.build_message("Continue the same session", False, "session")
    forbidden.assert_not_called()
    preferences.assert_called_once()
    lessons.assert_called_once()


@pytest.mark.parametrize(
    "options", [{"blocks_reads": True}, {"context_groups": frozenset({CONTEXT_GROUP_LESSONS})}]
)
def test_withheld_memory_does_not_advertise_automatic_recall(tmp_path, options):
    memory = MemoryStore(workspace=tmp_path / "workspace")
    memory.read_preferences = Mock(side_effect=AssertionError("withheld memory was read"))
    memory.read_projects = Mock(side_effect=AssertionError("withheld memory was read"))
    builder = ContextBuilder(
        memory=memory,
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        lessons=LessonStore(base_dir=tmp_path),
    )
    message, _ = builder.build_message("Current question", True, "session", **options)
    assert "[Memory tools]" not in message
    assert "Facts and past experiences are not searched automatically" not in message
