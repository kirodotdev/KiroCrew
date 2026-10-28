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
from datetime import datetime
from types import SimpleNamespace

from kiro_crew import context as context_module
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


class _FrozenClock(datetime):
    """A ``datetime`` whose ``now()`` does not advance.

    The render carries a minute-resolution timestamp, so two renders that
    straddle a minute boundary differ in that line alone.
    """

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return datetime(2026, 1, 1, 12, 0).replace(tzinfo=tz)


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

    def test_none_and_all_groups_are_identical(self, tmp_path, monkeypatch):
        """A parent passing no flag (all groups) == every other caller (None)."""
        monkeypatch.setattr(context_module, "datetime", _FrozenClock)
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
        # The activity half rides along as budgeted background.
        assert "## Active Projects" in ctx
        assert "memory_recall" in ctx

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
        monkeypatch.setattr("kiro_crew.context._build_docs_section", lambda: "[DOCS-SENTINEL]\n\n")
        ctx = _builder(tmp_path).build_session_context()
        assert "[DOCS-SENTINEL]" in ctx

    def test_absent_when_withheld(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.context._build_docs_section", lambda: "[DOCS-SENTINEL]\n\n")
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

    def test_remote_session_names_the_guest_directory_not_the_gateway_project(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew import session_environment as se

        se.remember_remote_workspace("s-remote", "Coder MicroVM", "/workspace/crew-session")
        # Force the resource line on so its gateway qualifier is exercised too.
        status = type(
            "S",
            (),
            {
                # The builder asks for the remote wording for a remote session.
                "context_line": lambda self, remote=False: (
                    "[RESOURCES] The gateway machine is tight (remote-scoped)."
                    if remote
                    else "[RESOURCES] Host memory is tight (~3 GB free)."
                ),
                "posture": "tight",
                "available_gb": 3.0,
            },
        )()
        monkeypatch.setattr("kiro_crew.resource_status.probe", lambda: status)
        try:
            b = _builder(tmp_path)
            msg, _ = b.build_message("hi", True, "s-remote", project="/Users/me/repo")
            off, _ = b.build_message(
                "hi",
                True,
                "s-remote",
                project="/Users/me/repo",
                context_groups=ALL_GROUPS - {CONTEXT_GROUP_PROJECT},
            )
        finally:
            se.forget_remote_workspace("s-remote")
        assert "remote environment (Coder MicroVM)" in msg
        assert "/workspace/crew-session" in msg
        assert "[PROJECT] Active project directory: /Users/me/repo" not in msg
        assert "[RESOURCES] The gateway machine is tight (remote-scoped)." in msg
        assert "Host memory is tight" not in msg
        assert "remote environment" not in off
        # Forgotten (switched back to local): the plain line returns.
        local, _ = b.build_message("hi", True, "s-remote", project="/Users/me/repo")
        assert "[PROJECT] Active project directory: /Users/me/repo" in local


class TestEpisodicMemoryGate:
    """Episodes ride in the budgeted activity block; both halves follow inheritance."""

    def _builder_with_episodic(self, tmp_path):
        builder = _builder(tmp_path)
        store = builder.get_memory_for(None)
        store._vector_store = SimpleNamespace(
            get_episodic_context=lambda query_text, cap: "[EPISODIC-SENTINEL]",
            get_semantic_context=lambda query_text, cap, facts_only=False: "",
            get_preferences_context=lambda query_text="", cap=0: "[PREFERENCE-SENTINEL]",
            get_lessons_context=lambda query_text, cap, project_dir=None, recall_query=None, background=False, hard_cap=0, directive_budget=0, experience_budget=0: "",
            startup_lesson_query=lambda query_text: None,
            has_any_lesson=lambda: True,
        )
        return builder

    def test_activity_rides_the_memory_group_once(self, tmp_path):
        builder = self._builder_with_episodic(tmp_path)
        msg, _ = builder.build_message("q", True, "s1")
        assert msg.count("[EPISODIC-SENTINEL]") == 1
        assert msg.count("[PREFERENCE-SENTINEL]") == 1
        assert "[EPISODIC-SENTINEL]" in builder.memory.get_context(query="q")

    def test_withheld_with_the_memory_group(self, tmp_path):
        msg, _ = self._builder_with_episodic(tmp_path).build_message(
            "q", True, "s1", context_groups=ALL_GROUPS - {CONTEXT_GROUP_MEMORY}
        )
        assert "[EPISODIC-SENTINEL]" not in msg
        assert "[PREFERENCE-SENTINEL]" not in msg
        assert "[Memory tools]" not in msg

    def test_survives_withholding_an_unrelated_group(self, tmp_path):
        msg, _ = self._builder_with_episodic(tmp_path).build_message(
            "q", True, "s1", context_groups=ALL_GROUPS - {CONTEXT_GROUP_PROJECT}
        )
        assert "[EPISODIC-SENTINEL]" in msg
        assert "[PREFERENCE-SENTINEL]" in msg


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


class TestSubagentCriticalRules:
    """A sub-agent's reply goes to its parent agent, not to a chat UI.

    The channel variant tells the model to end choices with an [OPTIONS:] line
    that "renders interactive buttons in the UI", which contradicts the spawn
    prefix's "Do NOT include [OPTIONS: ...]" and makes children end reports
    with button rows meant for a person.
    """

    _UI_CLAUSES = (
        "you MUST end your response",
        "renders interactive buttons",
        "UI file viewer panel",
        "click-to-copy chip",
        "option label",
        "the user can see what changed",
    )

    def test_subagent_key_gets_the_subagent_variant(self, tmp_path):
        ctx = _builder(tmp_path).build_session_context(session_key="subagent:abc")
        rules = context_module._CRITICAL_RULES_SUBAGENT
        assert ctx.startswith(rules)
        for clause in self._UI_CLAUSES:
            assert clause not in rules, clause
        assert "absolute path" in rules
        assert "Do not assume anyone's gender." in rules

    def test_other_runtimes_keep_their_variants(self):
        assert context_module._critical_rules_for("dashboard:x", None) == (
            context_module._CRITICAL_RULES
        )
        assert context_module._critical_rules_for("cron:x", None) == (
            context_module._CRITICAL_RULES_CHANNEL
        )

    def test_subagent_rules_survive_marker_neutralization(self, tmp_path):
        """build_message must recognise the new block as the trusted prefix,
        otherwise its own [END CRITICAL RULES] marker is scrubbed."""
        msg, _ = _builder(tmp_path).build_message(
            "sub task",
            True,
            session_key="subagent:abc",
            interactive=False,
        )
        # The final message folds the heading's em dash to "--", so check the
        # body and the closing marker rather than the raw constant.
        assert "Your reply goes to the agent that started you" in msg
        assert "[END CRITICAL RULES]" in msg
        assert "renders interactive buttons" not in msg


class TestSessionContextOpener:
    """The [SESSION CONTEXT] opener must not claim content the parent withheld."""

    _CLAIM = "This is your memory, lessons, and conversation history"

    def test_default_opener_names_memory_and_lessons(self, tmp_path):
        msg, _ = _builder(tmp_path).build_message("q", True, "subagent:abc", interactive=False)
        assert self._CLAIM in msg

    def test_neutral_opener_when_every_group_is_withheld(self, tmp_path):
        msg, _ = _builder(tmp_path).build_message(
            "q", True, "subagent:abc", interactive=False, context_groups=frozenset()
        )
        assert self._CLAIM not in msg
        assert "rules and runtime facts for this turn" in msg
        assert "CURRENT USER REQUEST below" in msg
        assert "[END OF SESSION CONTEXT]" in msg
