"""V1 preserves eager session recall; V2 leaves fragment retrieval to its tool."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from kiro_crew.context import CONTEXT_GROUP_LESSONS, ContextBuilder
from kiro_crew.learn import LessonStore
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader


def test_v2_prompt_lifecycles_leave_retrieval_to_the_tool(tmp_path):
    memory = MemoryStore(workspace=tmp_path / "workspace")
    memory.write_preferences("Use a concise reply.")
    memory.write_projects("The active project is Beacon.")
    forbidden = Mock(side_effect=AssertionError("prompt construction attempted retrieval"))
    rules = Mock(return_value="[Scoped correction: run the project checks.]")
    memory._vector_store = SimpleNamespace(
        algorithm_version="v2",
        recall=forbidden,
        get_semantic_context=forbidden,
        get_episodic_context=forbidden,
        has_any_lesson=lambda: True,
        get_lessons_context=rules,
    )
    memory.read_recent_history = forbidden
    builder = ContextBuilder(
        memory=memory,
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        lessons=LessonStore(base_dir=tmp_path),
    )
    first, _ = builder.build_message("Find our earlier deployment decision", True, "session")
    assert "Use a concise reply." in first
    assert "The active project is Beacon." in first
    assert "Scoped correction" in first and "memory_recall" in first
    for options in ({}, {"needs_reinjection": True}):
        builder.build_message("Now another topic", False, "session", **options)
    builder.build_message("Continue after restart", True, "session", resumed=True)
    forbidden.assert_not_called()
    assert rules.call_args_list
    assert all(call.kwargs["query_text"] == "" for call in rules.call_args_list)


def test_v1_new_session_keeps_history_and_query_ranked_retrieval(tmp_path):
    memory = MemoryStore(workspace=tmp_path / "workspace")
    memory.write_preferences("Use a concise reply.")
    memory.write_projects("The active project is Beacon.")
    memory.read_recent_history = Mock(return_value="Earlier daily history sentinel")
    semantic = Mock(return_value="[Semantic Memory]\nEarlier fact sentinel")
    episodic = Mock(return_value="[Episodic Memory]\nEarlier event sentinel")
    lessons = Mock(return_value="[Lessons Learned]\nRelevant correction sentinel")
    memory._vector_store = SimpleNamespace(
        algorithm_version="v1",
        get_semantic_context=semantic,
        get_episodic_context=episodic,
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
        "The active project is Beacon.",
        "Earlier daily history sentinel",
        "Earlier fact sentinel",
        "Earlier event sentinel",
        "Relevant correction sentinel",
    ):
        assert sentinel in first
    assert "[Memory tools]" not in first
    memory.read_recent_history.assert_called_once()
    semantic.assert_called_once()
    episodic.assert_called_once()
    lessons.assert_called_once()
    assert semantic.call_args.kwargs["query_text"] == query
    assert episodic.call_args.kwargs["query_text"] == query
    assert lessons.call_args.kwargs["query_text"] == query
    assert episodic.call_args.kwargs["cap"] == 3000

    builder.build_message("Continue the same session", False, "session")
    memory.read_recent_history.assert_called_once()
    semantic.assert_called_once()
    episodic.assert_called_once()
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
