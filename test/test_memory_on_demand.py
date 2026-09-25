"""Both memory versions leave history and fragment retrieval to the recall tool."""

from types import SimpleNamespace
from unittest.mock import ANY, Mock

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


def test_v1_new_session_reads_activity_once_and_follow_ups_never(tmp_path):
    memory = MemoryStore(workspace=tmp_path / "workspace")
    memory.write_preferences("Use a concise reply.")
    memory.write_projects("The active project is Beacon.")
    history = Mock(return_value="#### 09:00 deploy\nHistory sentinel")
    memory.read_recent_history = history
    preferences = Mock(return_value="[Semantic Memory]\nPreference fact sentinel")
    facts = Mock(return_value="[Task facts]\nTask fact sentinel")
    episodes = Mock(return_value="[Episodic Memory]\nEpisode sentinel")
    lessons = Mock(return_value="[Lessons Learned]\nRelevant correction sentinel")
    memory._vector_store = SimpleNamespace(
        algorithm_version="v1",
        get_semantic_context=facts,
        get_episodic_context=episodes,
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
        "Task fact sentinel",
        "Episode sentinel",
        "Relevant correction sentinel",
        "[End of memory activity index]",
    ):
        assert first.count(sentinel) == 1, sentinel
    # Named by the activity index AND carried whole in the activity block.
    for sentinel in ("The active project is Beacon.", "History sentinel", "memory_recall"):
        assert sentinel in first, sentinel
    preferences.assert_called_once()
    lessons.assert_called_once()
    # Facts and episodes are ranked against the request, once each, with the
    # pref.* rows left to the protected preferences read.
    facts.assert_called_once_with(query_text=query, cap=ANY, facts_only=True)
    episodes.assert_called_once_with(query_text=query, cap=ANY)
    # One history read, by the activity block: the activity index reads the
    # uncached path and the protected preferences read skips history entirely.
    history.assert_called_once()

    builder.build_message("Continue the same session", False, "session")
    preferences.assert_called_once()
    lessons.assert_called_once()
    facts.assert_called_once()
    episodes.assert_called_once()
    history.assert_called_once()


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


def test_v1_new_session_bounds_pref_rows_at_the_startup_cap(tmp_path):
    """The cap must reach the store through the real plumbing
    (context._ResolvedCaps -> memory.get_context -> get_preferences_context).
    Mocks cannot see a dropped keyword argument; a real store over the cap can."""
    from kiro_crew.context import _PREFS_STARTUP_CAP
    from kiro_crew.vector_memory import VectorMemoryStore

    vectors = VectorMemoryStore(db_path=tmp_path / "mem.db")
    vectors.init()
    for i in range(60):
        vectors.set_semantic(f"pref.rule_{i:02d}", f"standing rule {i} " * 25, 0.9, "user_explicit")
    memory = MemoryStore(workspace=tmp_path / "workspace", vector_store=vectors)
    builder = ContextBuilder(
        memory=memory,
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        lessons=LessonStore(base_dir=tmp_path),
    )
    first, _ = builder.build_message("plan the deploy", True, "session")
    start = first.index("[Semantic Memory")
    end = first.index("[End of semantic memory]\n") + len("[End of semantic memory]\n")
    block = first[start:end]
    assert len(block) <= _PREFS_STARTUP_CAP
    assert "preference facts above the" in block
    assert f"{_PREFS_STARTUP_CAP}-character startup budget" in block
    assert 0 < block.count("\npref.rule_") < 60
