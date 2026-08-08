"""Tests for parent-selected sub-agent context groups.

A spawning parent decides which switchable groups (``memory`` / ``lessons`` /
``project``) its sub-agent inherits. Two properties matter:

1. **Default is today's behaviour.** ``context_groups=None`` — what every
   non-sub-agent caller passes, and what a parent that sets no flag produces —
   must yield byte-identical context.
2. **Omitting a group removes its sections entirely**, not as a zero cap. A zero
   cap emits a truncation marker rather than an empty string, which is how the
   earlier attempt leaked section headers with no content.

Each group has a test asserting presence-with and absence-without, so reverting
that group's gate fails exactly one test.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from kiro_crew.config.loader import config_path
from kiro_crew.context import (
    CONTEXT_GROUP_LESSONS,
    CONTEXT_GROUP_MEMORY,
    CONTEXT_GROUP_PROJECT,
    SWITCHABLE_CONTEXT_GROUPS,
    ContextBuilder,
)
from kiro_crew.learn import Lesson, LessonStore
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader

ALL_GROUPS = frozenset(SWITCHABLE_CONTEXT_GROUPS)


def _builder(tmp_path) -> ContextBuilder:
    """A builder with every switchable group's content actually populated.

    Without seeded content an "absent" assertion passes for the wrong reason.
    """
    memory = MemoryStore(workspace=tmp_path / "ws")
    memory.write_preferences("# User Preferences\n\n- Prefers tabs over spaces\n")
    memory.write_projects("# Active Projects\n\n- Ship the widget rewrite\n")
    lessons = LessonStore(base_dir=tmp_path)
    lessons.save(
        Lesson(ts="2026-01-01T00:00:00Z", rule="Always pass encoding=utf-8", category="tool")
    )
    # Onboarding answers put a [USER PROFILE] block in the lessons group.
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"dashboard": {"user_role": "developer"}}),
        encoding="utf-8",
    )
    return ContextBuilder(
        memory=memory,
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        lessons=lessons,
    )


class TestDefaultIsUnchanged:
    """The all-on paths must not drift from the pre-feature builder."""

    def test_none_and_all_groups_are_identical(self, tmp_path):
        """A parent passing no flag (all groups) == every other caller (None)."""
        b = _builder(tmp_path)
        assert b.build_session_context(context_groups=None) == b.build_session_context(
            context_groups=ALL_GROUPS
        )

    def test_no_scope_marker_when_nothing_withheld(self, tmp_path):
        b = _builder(tmp_path)
        assert "[CONTEXT SCOPE]" not in b.build_session_context(context_groups=None)
        assert "[CONTEXT SCOPE]" not in b.build_session_context(context_groups=ALL_GROUPS)

    def test_conduct_survives_every_opt_out(self, tmp_path):
        """Critical rules and the skills index are not switchable."""
        ctx = _builder(tmp_path).build_session_context(context_groups=frozenset())
        assert "[CRITICAL RULES" in ctx
        assert "[CURRENT DATE]" in ctx
        assert "[WORKSPACE IDENTITY]" in ctx


class TestMemoryGroup:
    def test_present_by_default(self, tmp_path):
        ctx = _builder(tmp_path).build_session_context()
        assert "## User Preferences" in ctx
        assert "Prefers tabs over spaces" in ctx
        assert "## Active Projects" in ctx

    def test_absent_when_withheld(self, tmp_path):
        ctx = _builder(tmp_path).build_session_context(
            context_groups=ALL_GROUPS - {CONTEXT_GROUP_MEMORY}
        )
        assert "## User Preferences" not in ctx
        assert "Prefers tabs over spaces" not in ctx
        assert "## Active Projects" not in ctx

    def test_withheld_leaves_no_truncation_marker(self, tmp_path):
        """The zero-cap failure mode: a header/marker with no content behind it."""
        ctx = _builder(tmp_path).build_session_context(
            context_groups=ALL_GROUPS - {CONTEXT_GROUP_MEMORY}
        )
        assert "…[truncated]" not in ctx

    def test_other_groups_unaffected(self, tmp_path):
        ctx = _builder(tmp_path).build_session_context(
            context_groups=ALL_GROUPS - {CONTEXT_GROUP_MEMORY}
        )
        assert "Always pass encoding=utf-8" in ctx


class TestLessonsGroup:
    def test_present_by_default(self, tmp_path):
        ctx = _builder(tmp_path).build_session_context()
        assert "Always pass encoding=utf-8" in ctx
        assert "[USER PROFILE]" in ctx

    def test_absent_when_withheld(self, tmp_path):
        ctx = _builder(tmp_path).build_session_context(
            context_groups=ALL_GROUPS - {CONTEXT_GROUP_LESSONS}
        )
        assert "Always pass encoding=utf-8" not in ctx
        assert "[USER PROFILE]" not in ctx

    def test_other_groups_unaffected(self, tmp_path):
        ctx = _builder(tmp_path).build_session_context(
            context_groups=ALL_GROUPS - {CONTEXT_GROUP_LESSONS}
        )
        assert "Prefers tabs over spaces" in ctx


class TestProjectGroup:
    """The docs section is patched to a sentinel on purpose.

    ``_build_docs_section()`` returns "" when the bundled docs directory is
    absent, so asserting on the real block would pass for the wrong reason on a
    host without it — both "present" and "absent" would hold with the gate
    removed. The sentinel makes the gate the only thing under test.
    """

    def test_present_by_default(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.context._build_docs_section", lambda: "[DOCS-SENTINEL]\n\n"
        )
        ctx = _builder(tmp_path).build_session_context()
        assert "[DOCS-SENTINEL]" in ctx

    def test_absent_when_withheld(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.context._build_docs_section", lambda: "[DOCS-SENTINEL]\n\n"
        )
        ctx = _builder(tmp_path).build_session_context(
            context_groups=ALL_GROUPS - {CONTEXT_GROUP_PROJECT}
        )
        assert "[DOCS-SENTINEL]" not in ctx

    def test_project_dir_line_gated_in_build_message(self, tmp_path):
        b = _builder(tmp_path)
        on, _ = b.build_message("do the thing", True, "s1", project="/tmp/proj")
        off, _ = b.build_message(
            "do the thing",
            True,
            "s1",
            project="/tmp/proj",
            context_groups=ALL_GROUPS - {CONTEXT_GROUP_PROJECT},
        )
        assert "[PROJECT] Active project directory: /tmp/proj" in on
        assert "[PROJECT] Active project directory" not in off


class TestEpisodicMemoryGate:
    """Episodic memory lives in build_message, not build_session_context.

    It is the one memory-group section injected on the per-message path, so a
    gate that covered only build_session_context would leave it flowing to a
    sub-agent whose parent withheld memory. The vector store is stubbed so the
    assertion is about the gate, not about embedding availability.
    """

    def _builder_with_episodic(self, tmp_path):
        builder = _builder(tmp_path)
        store = builder.get_memory_for(None)
        store._vector_store = SimpleNamespace(
            get_episodic_context=lambda query_text, cap: "[EPISODIC-SENTINEL]",
            get_semantic_context=lambda query_text, cap: "",
            get_lessons_context=lambda: "",
        )
        return builder

    def test_injected_by_default(self, tmp_path):
        msg, _ = self._builder_with_episodic(tmp_path).build_message("q", True, "s1")
        assert "[EPISODIC-SENTINEL]" in msg

    def test_withheld_with_the_memory_group(self, tmp_path):
        msg, _ = self._builder_with_episodic(tmp_path).build_message(
            "q", True, "s1", context_groups=ALL_GROUPS - {CONTEXT_GROUP_MEMORY}
        )
        assert "[EPISODIC-SENTINEL]" not in msg

    def test_survives_withholding_an_unrelated_group(self, tmp_path):
        msg, _ = self._builder_with_episodic(tmp_path).build_message(
            "q", True, "s1", context_groups=ALL_GROUPS - {CONTEXT_GROUP_PROJECT}
        )
        assert "[EPISODIC-SENTINEL]" in msg


class TestContextScopeMarker:
    """A withheld group must be named, or the sub-agent invents what it lacks."""

    def test_names_the_withheld_group(self, tmp_path):
        ctx = _builder(tmp_path).build_session_context(
            context_groups=ALL_GROUPS - {CONTEXT_GROUP_MEMORY}
        )
        assert "[CONTEXT SCOPE]" in ctx
        assert "memory (user preferences, projects, prior sessions)" in ctx
        assert "do not guess" in ctx

    def test_names_every_withheld_group(self, tmp_path):
        ctx = _builder(tmp_path).build_session_context(context_groups=frozenset())
        for group in SWITCHABLE_CONTEXT_GROUPS:
            assert group in ctx.split("[End of context scope]")[0]

    def test_mentions_only_withheld_groups(self, tmp_path):
        scope = (
            _builder(tmp_path)
            .build_session_context(context_groups=ALL_GROUPS - {CONTEXT_GROUP_PROJECT})
            .split("[End of context scope]")[0]
        )
        assert "project (" in scope
        assert "memory (" not in scope
        assert "lessons (" not in scope
