"""Tests for the memory and learned-lesson blocks ``ContextBuilder`` injects.

This module pins how a session start reads memory and lessons: the startup lesson
cap, the episodic-query skip branches with the static guard that keeps
``build_message`` off the event loop, the ``get_context`` query wiring and the
vector-store and JSONL lesson fallback, unreadable notebook files at session
start, and durable model-version references in lessons. The core turn shape
lives in ``test_context.py`` and the prompt-text blocks in
``test_context_prompt_text.py``.
"""

from __future__ import annotations

import re
from unittest.mock import patch

import pytest

from conftest import plant_day_link
from kiro_crew.context import ContextBuilder
from kiro_crew.learn import LessonStore
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader

# One xdist worker for the whole module. The ``build_message`` call-site ratchet below
# walks the package through the shared ``source_corpus`` (one memoised file list per
# process, texts streamed and parsed only for the files that can match), so its cost is
# now a second rather than the ~30s full-tree ``ast.parse`` this group was first added
# for; the group stays so the file-list cache is paid once per run, not once per worker.
pytestmark = pytest.mark.xdist_group(name="tree_scan_test_context")


@pytest.fixture(autouse=True)
def _close_skills_loaders(close_skills_loaders):
    """Every test here builds a ``ContextBuilder``: close its ``SkillsLoader`` (``test/conftest.py``)."""


class TestLessonsCap:
    def test_over_cap_preserves_complete_explicit_rules(self, tmp_path):
        from kiro_crew.context import _LESSONS_STARTUP_CAP
        from kiro_crew.learn import Lesson

        lessons = LessonStore(base_dir=tmp_path)
        # Save enough long lessons that the formatted context exceeds the cap.
        # The budget that BINDS the startup rule tier is ``_LESSONS_STARTUP_CAP``
        # (the window-independent authored-tier allowance passed as the startup
        # renderers' ``directive_budget``), not the ordinary ``_LESSONS_CAP``, so
        # the fixture is sized to overflow that one.
        rule = "x" * 1000
        for i in range(_LESSONS_STARTUP_CAP // 1000 + 5):
            lessons.save(Lesson(ts=str(i), rule=f"{i}-{rule}", category="knowledge"))

        builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
            lessons=lessons,
        )
        ctx = builder.build_session_context()

        assert "CRITICAL ERROR — LESSONS FILE TOO LARGE" not in ctx
        assert "[lessons truncated]" not in ctx
        # The rule budget BINDS on the startup path. What it must never do is emit
        # a partial rule: trimming is by whole entry, so every rule that appears
        # appears in full, and the ones that did not fit are reported with exact
        # counts instead of vanishing.
        total = _LESSONS_STARTUP_CAP // 1000 + 5
        # Match the whole rendered entry, not the rule text: these fixture rules
        # are prefix-ambiguous ("0-xxx…" is a substring of "10-xxx…"), so a bare
        # ``in`` reports a rule as present that was never emitted. Anchoring on the
        # "- " bullet and the terminating newline both disambiguates the index AND
        # proves the entry is complete rather than a truncated prefix.
        entries = {i: f"- {i}-{rule}\n" for i in range(total)}
        present = [i for i, entry in entries.items() if entry in ctx]
        assert present, "the rule budget must still admit rules"
        assert len(present) < total, "this fixture is sized to overflow the rule budget"
        # FULL ACCOUNTING, which is the invariant a subset check does not carry: a
        # subset assertion holds even if rules vanish, so the count the prompt
        # reports as omitted must exactly equal the count missing from the prompt.
        # Every rule is then either rendered in full or named in the notice, and a
        # rule cannot disappear unaccounted for.
        notice = re.search(r"omitted (\d+) of (\d+) retained rules", ctx)
        assert notice, "an overflow must report itself"
        omitted, reported_total = int(notice.group(1)), int(notice.group(2))
        assert reported_total == total
        assert len(present) + omitted == total
        assert "read them with learn_list" in ctx
        # And the block stays inside the budget it names.
        start = ctx.index("[Learned corrections")
        end = ctx.index("[End of learned corrections]", start)
        assert end - start <= _LESSONS_STARTUP_CAP

    def test_under_cap_no_error_block(self, tmp_path):
        from kiro_crew.learn import Lesson

        lessons = LessonStore(base_dir=tmp_path)
        lessons.save(Lesson(ts="1", rule="always run the formatter", category="knowledge"))
        lessons.save(Lesson(ts="2", rule="never force push to mainline", category="knowledge"))

        builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
            lessons=lessons,
        )
        ctx = builder.build_session_context()

        # Under the cap: no error block, and ALL lessons preserved verbatim.
        assert "CRITICAL ERROR — LESSONS FILE TOO LARGE" not in ctx
        assert "always run the formatter" in ctx
        assert "never force push to mainline" in ctx
        assert "[lessons truncated]" not in ctx


class TestBuildMessageOffloadedAtCallSites:
    """build_message embeds the episodic query via a blocking urllib call to
    Ollama on new sessions. Async callers (gateway loop coroutines) wrap the
    whole call in run_in_embed_pool — enforced statically by
    TestAsyncCallSitesUseToThread below. These tests cover the skip branches
    (follow-up / minimal-context must never embed) and that build_message
    remains sync-callable (CLI, 25+ existing tests, and MagicMock-based test
    doubles all rely on the sync signature).
    """

    def _builder(self, tmp_path):
        return ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
            lessons=LessonStore(base_dir=tmp_path),
        )

    def test_follow_up_skips_episodic_entirely(self, tmp_path):
        from unittest.mock import MagicMock

        vector_store = MagicMock()
        builder = self._builder(tmp_path)
        fake_memory = MagicMock()
        fake_memory.vector_store = vector_store
        fake_memory.get_context.return_value = ""
        fake_memory.activity_index.return_value = ""
        fake_memory.get_activity_context.return_value = ""
        vector_store.get_lessons.return_value = []

        with patch.object(ContextBuilder, "get_memory_for", return_value=fake_memory):
            builder.build_message("follow up message", False, "sess-1")

        vector_store.get_episodic_context.assert_not_called()

    def test_minimal_context_skips_episodic(self, tmp_path):
        from unittest.mock import MagicMock

        vector_store = MagicMock()
        builder = self._builder(tmp_path)
        fake_memory = MagicMock()
        fake_memory.vector_store = vector_store
        fake_memory.get_context.return_value = ""
        fake_memory.activity_index.return_value = ""
        fake_memory.get_activity_context.return_value = ""
        vector_store.get_lessons.return_value = []
        vector_store.get_semantic_context.return_value = ""

        with patch.object(ContextBuilder, "get_memory_for", return_value=fake_memory):
            builder.build_message("message", True, "sess-1", minimal_context=True)

        vector_store.get_episodic_context.assert_not_called()


class TestAsyncCallSitesUseToThread:
    """Static guard: no async coroutine may call build_message inline.

    Every production call site of ``build_message`` inside an ``async def``
    must go through ``run_in_embed_pool`` / an executor offload (the episodic
    query embed blocks). This walks the AST of all gateway-process modules so
    a future call site reintroducing the inline pattern fails CI rather than
    shipping a fourth loop-stall bug (22475ceb, _save_lessons, build_message
    were the first three).

    Scope rules mirror test_no_blocking_call_on_loop.py: a nested ``def`` /
    ``async def`` / ``lambda`` is a separate frame (a sync helper, a thread
    target, an offloaded callable such as ``run_in_executor(None, lambda:
    ctx.build_message(...))``) and is NOT scanned as part of the enclosing
    coroutine — so both sanctioned offload shapes pass. A ``# loop-ok:
    <reason>`` trailing comment suppresses a finding.
    """

    def test_no_inline_build_message_in_async_functions(self):
        import ast

        from source_corpus import parsed_candidates, src_root

        nested_scopes = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
        root = src_root()
        offenders: list[str] = []

        def _iter_frame_calls(fn: ast.AsyncFunctionDef):
            """Yield Call nodes lexically in *fn*'s own frame — skip nested scopes."""
            stack: list[ast.AST] = list(ast.iter_child_nodes(fn))
            while stack:
                node = stack.pop()
                if isinstance(node, nested_scopes):
                    continue  # separate frame: sync helper / thread target / lambda
                if isinstance(node, ast.Call):
                    yield node
                stack.extend(ast.iter_child_nodes(node))

        # A finding is an ``ast.Attribute`` whose ``attr`` is ``build_message``, so
        # that identifier cannot be absent from an offending file's text: the shared
        # corpus parses only the files that carry it (a few dozen, not the whole
        # package), one tree at a time, which is what took this gate from an
        # 11-second full-tree parse to well under a second. ONLY that needle: the
        # coroutine itself is found by the AST (``ast.AsyncFunctionDef``), never by
        # a text needle -- ``async  def`` with two spaces is a valid coroutine that a
        # literal ``"async def"`` filter would skip, and a filter that can skip a
        # valid offender is a gate that fails open.
        for py, text, tree in parsed_candidates(require_all=("build_message",)):
            lines = text.splitlines()
            for fn in ast.walk(tree):
                if not isinstance(fn, ast.AsyncFunctionDef):
                    continue
                for call in _iter_frame_calls(fn):
                    func = call.func
                    # Inline call: the Call's func IS .build_message. The
                    # sanctioned run_in_embed_pool(x.build_message, ...) form
                    # passes the method as an ARG, so its Call func is
                    # to_thread and never matches here.
                    if not (isinstance(func, ast.Attribute) and func.attr == "build_message"):
                        continue
                    src_line = lines[call.lineno - 1] if call.lineno <= len(lines) else ""
                    if "# loop-ok" in src_line:
                        continue
                    offenders.append(f"{py.relative_to(root)}:{call.lineno} in async {fn.name}")

        assert not offenders, (
            "build_message called inline from async coroutine(s) — the episodic "
            "query embed blocks the event loop; wrap in run_in_embed_pool (or "
            "add '# loop-ok: <reason>' if genuinely safe):\n  " + "\n  ".join(offenders)
        )


class TestMemoryGetContextQueryWiring:
    """Startup reads preferences protected (activity off) and the activity block
    as budgeted background; explicit readers retain the combined read."""

    def _builder(self, tmp_path):
        return ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
            lessons=LessonStore(base_dir=tmp_path),
        )

    def test_new_session_passes_query_to_get_context(self, tmp_path):
        from unittest.mock import MagicMock

        builder = self._builder(tmp_path)
        fake_memory = MagicMock()
        fake_memory.get_context.return_value = ""
        fake_memory.activity_index.return_value = ""
        fake_memory.get_activity_context.return_value = ""
        fake_memory.vector_store = None

        with patch.object(ContextBuilder, "get_memory_for", return_value=fake_memory):
            builder.build_message("what did we decide about paris", True, "sess-1")

        assert fake_memory.get_context.call_count == 1
        kwargs = fake_memory.get_context.call_args.kwargs
        assert kwargs["query"] == "what did we decide about paris"
        assert kwargs["include_activity"] is False

    def test_an_empty_scoped_lesson_result_does_not_fall_back_to_jsonl(self, tmp_path):
        # A POPULATED vector store whose rows are all out of scope has already
        # answered. Falling through would let the JSONL store speak for it and
        # re-inject rows deleted from it.
        from types import SimpleNamespace

        from kiro_crew.learn import Lesson

        builder = self._builder(tmp_path)
        store = builder.get_memory_for(None)
        store._vector_store = SimpleNamespace(
            get_episodic_context=lambda query_text, cap: "",
            get_semantic_context=lambda query_text, cap, facts_only=False: "",
            get_preferences_context=lambda query_text="", cap=0: "",
            get_lessons_context=lambda query_text, cap, project_dir=None, recall_query=None, background=False, hard_cap=0, directive_budget=0, experience_budget=0: "",
            startup_lesson_query=lambda query_text: None,
            has_any_lesson=lambda: True,
        )
        builder.lessons.save(Lesson(ts="t", rule="JSONL-SENTINEL", category="tool"))
        msg, _ = builder.build_message("q", True, "s1")
        assert "JSONL-SENTINEL" not in msg

    def test_an_unpopulated_vector_store_still_yields_jsonl_lessons(self, tmp_path):
        # The opposite failure: while a first-boot migration is still filling the
        # vector store it exists but holds nothing, and saved corrections must not
        # vanish from the prompt in the meantime.
        from types import SimpleNamespace

        from kiro_crew.learn import Lesson

        builder = self._builder(tmp_path)
        store = builder.get_memory_for(None)
        store._vector_store = SimpleNamespace(
            get_episodic_context=lambda query_text, cap: "",
            get_semantic_context=lambda query_text, cap, facts_only=False: "",
            get_preferences_context=lambda query_text="", cap=0: "",
            get_lessons_context=lambda query_text, cap, project_dir=None, recall_query=None, background=False, hard_cap=0, directive_budget=0, experience_budget=0: "",
            startup_lesson_query=lambda query_text: None,
            has_any_lesson=lambda: False,
        )
        builder.lessons.save(Lesson(ts="t", rule="JSONL-SENTINEL", category="tool"))
        msg, _ = builder.build_message("q", True, "s1")
        assert "JSONL-SENTINEL" in msg

    def test_withheld_only_vector_store_still_yields_jsonl_lessons(self, tmp_path):
        from kiro_crew.learn import Lesson
        from kiro_crew.vector_memory import VectorMemoryStore

        builder = self._builder(tmp_path)
        memory = builder.get_memory_for(None)
        vector_store = VectorMemoryStore(db_path=tmp_path / "vectors.db", embedding_dim=4)
        vector_store.init()
        try:
            memory._vector_store = vector_store
            vector_store.set_semantic(
                "lesson.legacyvolatile",
                {
                    "rule": "The current model identity is gpt-5.6-sol.",
                    "category": "preference",
                    "negative": None,
                },
                1.0,
                "user_explicit",
            )
            builder.lessons.save(Lesson(ts="t", rule="JSONL-SENTINEL", category="tool"))

            msg, _ = builder.build_message("q", True, "s1")

            assert "JSONL-SENTINEL" in msg
            assert "gpt-5.6-sol" not in msg
        finally:
            vector_store.close()

    def test_episodic_is_only_included_by_explicit_memory_reader(self, tmp_path):
        from types import SimpleNamespace

        builder = self._builder(tmp_path)
        store = builder.get_memory_for(None)
        store._vector_store = SimpleNamespace(
            get_episodic_context=lambda query_text, cap: "[EPISODIC-SENTINEL]",
            get_semantic_context=lambda query_text, cap, facts_only=False: "",
            get_preferences_context=lambda query_text="", cap=0: "",
            get_lessons_context=lambda query_text, cap, project_dir=None, recall_query=None, background=False, hard_cap=0, directive_budget=0, experience_budget=0: "",
            startup_lesson_query=lambda query_text: None,
            has_any_lesson=lambda: True,
        )
        msg, _ = builder.build_message("q", True, "s1")
        # The protected preferences read (include_activity=False) never builds
        # episodes; the budgeted activity block does, exactly once.
        assert msg.count("[EPISODIC-SENTINEL]") == 1
        assert "[EPISODIC-SENTINEL]" not in store.get_context(query="q", include_activity=False)
        assert store.get_context(query="q").count("[EPISODIC-SENTINEL]") == 1

    def test_episodic_query_is_the_user_message(self, tmp_path):
        from types import SimpleNamespace

        seen: list[str] = []
        builder = self._builder(tmp_path)
        store = builder.get_memory_for(None)

        def _episodic(query_text, cap):
            seen.append(query_text)
            return ""

        store._vector_store = SimpleNamespace(
            get_episodic_context=_episodic,
            get_semantic_context=lambda query_text, cap, facts_only=False: "",
            get_preferences_context=lambda query_text="", cap=0: "",
            get_lessons_context=lambda query_text, cap, project_dir=None, recall_query=None, background=False, hard_cap=0, directive_budget=0, experience_budget=0: "",
            startup_lesson_query=lambda query_text: None,
            has_any_lesson=lambda: True,
        )
        builder.build_message("find my tokyo notes", True, "s2")
        assert seen == ["find my tokyo notes"]
        store.get_context(query="find my tokyo notes")
        assert seen == ["find my tokyo notes", "find my tokyo notes"]

    def test_activity_block_is_background_not_protected(self, tmp_path):
        # A long history must be droppable by the admission loop, so it enters
        # the discretionary pool and never the protected set.
        from types import SimpleNamespace

        builder = self._builder(tmp_path)
        store = builder.get_memory_for(None)
        store._vector_store = SimpleNamespace(
            get_episodic_context=lambda query_text, cap: "",
            get_semantic_context=lambda query_text, cap, facts_only=False: "",
            get_preferences_context=lambda query_text="", cap=0: "",
            get_lessons_context=lambda query_text, cap, project_dir=None, recall_query=None, background=False, hard_cap=0, directive_budget=0, experience_budget=0: "",
            startup_lesson_query=lambda query_text: None,
            has_any_lesson=lambda: True,
        )
        huge = "ACTIVITY-FILLER " * 10_000
        with patch.object(type(store), "get_activity_context", return_value=huge):
            msg, _ = builder.build_message("q", True, "s3")
        assert "ACTIVITY-FILLER" not in msg
        assert "omitted background context" in msg


class TestUnreadableActivityAtSessionStart:
    """An unreadable notebook file must not abort the session-start build.

    ``build_session_context`` reads ``activity_index`` BEFORE the tolerant
    activity sections, so a projects file that raises ``OSError`` (a directory
    in its place, a permission denial) has to be skipped there too; a raise at
    that call loses the protected preferences with it. A single unreadable
    history day is skipped by the per-day read itself, and a history failure
    that is not tied to one day file is skipped by the whole-window guard.
    """

    # The sentinels below ride in background blocks the admission loop may drop
    # under host memory pressure, so the host's free memory is pinned off.
    pytestmark = pytest.mark.usefixtures("ample_host_resources")

    def _builder(self, tmp_path):
        return ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
            lessons=LessonStore(base_dir=tmp_path),
        )

    def test_history_day_replaced_by_a_directory_keeps_preferences(self, tmp_path):
        from datetime import date, timedelta

        builder = self._builder(tmp_path)
        store = builder.get_memory_for(None)
        store.init()
        store.write_preferences("# User Preferences\n\n- PREF_SENTINEL\n")
        store.write_projects("PROJECT_SENTINEL")
        store.append_history("VALID_HISTORY_SENTINEL")
        # flake-ok: pre-existing local-date read for the history window, moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-naive-local-time -- pre-existing local-date read for the history window, moved verbatim; tracked for a de-flake pass
        unreadable_day = (date.today() - timedelta(days=1)).isoformat()
        (store._history_dir / f"{unreadable_day}.md").mkdir()

        ctx = builder.build_session_context()

        assert "PREF_SENTINEL" in ctx
        assert "PROJECT_SENTINEL" in ctx
        # The per-day read skips only the poisoned day; the valid day rides.
        assert "## Recent History" in ctx
        assert "VALID_HISTORY_SENTINEL" in ctx

    def test_history_day_link_keeps_preferences_without_target(self, tmp_path):
        """Session startup skips a linked history day without exposing its target."""
        from datetime import date, timedelta

        builder = self._builder(tmp_path)
        store = builder.get_memory_for(None)
        store.init()
        store.write_preferences("# User Preferences\n\n- PREF_SENTINEL\n")
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "secret.txt"
        secret.write_text("SECRET_SENTINEL", encoding="utf-8")
        # flake-ok: pre-existing local-date read for the history window, moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-naive-local-time -- pre-existing local-date read for the history window, moved verbatim; tracked for a de-flake pass
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        plant_day_link(store._history_dir / f"{yesterday}.md", secret)
        store.append_history("VALID_HISTORY_SENTINEL")

        ctx = builder.build_session_context()

        assert "SECRET_SENTINEL" not in ctx
        assert "PREF_SENTINEL" in ctx
        assert "VALID_HISTORY_SENTINEL" in ctx

    def test_history_window_unreadable_keeps_preferences(self, tmp_path, monkeypatch):
        """A history failure not tied to one day file skips only history."""
        builder = self._builder(tmp_path)
        store = builder.get_memory_for(None)
        store.init()
        store.write_preferences("# User Preferences\n\n- PREF_SENTINEL\n")
        store.write_projects("PROJECT_SENTINEL")
        store.append_history("VALID_HISTORY_SENTINEL")

        def _unreadable(*args, **kwargs):
            raise PermissionError("history directory is unreadable")

        monkeypatch.setattr(store, "_read_recent_history_uncached", _unreadable)

        ctx = builder.build_session_context()

        assert "PREF_SENTINEL" in ctx
        assert "PROJECT_SENTINEL" in ctx
        assert "## Recent History" not in ctx
        assert "VALID_HISTORY_SENTINEL" not in ctx

    def test_projects_file_replaced_by_a_directory_keeps_preferences(self, tmp_path):
        builder = self._builder(tmp_path)
        store = builder.get_memory_for(None)
        store.init()
        store.write_preferences("# User Preferences\n\n- PREF_SENTINEL\n")
        store.append_history("HISTORY_SENTINEL")
        store._projects_file.unlink()
        store._projects_file.mkdir()

        ctx = builder.build_session_context()

        assert "PREF_SENTINEL" in ctx
        assert "HISTORY_SENTINEL" in ctx
        assert "## Active Projects" not in ctx

    def test_projects_file_link_keeps_preferences_without_target(self, tmp_path):
        """Session startup omits a linked ``projects.md`` without exposing its target."""
        builder = self._builder(tmp_path)
        store = builder.get_memory_for(None)
        store.init()
        store.write_preferences("# User Preferences\n\n- PREF_SENTINEL\n")
        store.append_history("HISTORY_SENTINEL")
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "secret.txt"
        secret.write_text("SECRET_SENTINEL", encoding="utf-8")
        store._projects_file.unlink()
        plant_day_link(store._projects_file, secret)

        ctx = builder.build_session_context()

        assert "SECRET_SENTINEL" not in ctx
        assert "PREF_SENTINEL" in ctx
        assert "HISTORY_SENTINEL" in ctx
        assert "## Active Projects" not in ctx


class TestDurableModelVersionLessonContext:
    RULE = "the Python 3.12 wheel needs claude-opus-4.8 pinned in role_models"
    NEGATIVE = "assume gpt-5.6-sol supports the streaming flag"
    MODEL_TOOLING_RULES = (
        ("Always use the tokenizer shipped with gpt-5.6-sol", "tool"),
        ("Prefer the retry wrapper when calling opus-4.8 endpoints", "preference"),
        ("Never use the streaming flag with gpt-5.6-sol", "tool"),
        ("Always use the gpt-5-compatible tokenizer", "tool"),
        ("Always use the gpt-5 compatible tokenizer", "tool"),
        ("Never use gpt-5.6-sol endpoints without the retry wrapper", "tool"),
    )
    BACKEND_PROCESS_RULES = (
        ("Never run as the backend service account", "tool"),
        ("The service is running as the backend worker", "preference"),
        ("The service is running as the active backend service account", "tool"),
        ("The service is running as the current backend worker", "preference"),
        ("Restart the backend after config changes", "tool"),
        ("Run the worker as the backend user, never as root", "preference"),
    )
    TOOLING_RULES = MODEL_TOOLING_RULES + BACKEND_PROCESS_RULES

    def _builder(self, tmp_path):
        return ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
            lessons=LessonStore(base_dir=tmp_path),
        )

    def test_jsonl_context_renders_durable_model_version_references(self, tmp_path):
        from types import SimpleNamespace

        from kiro_crew.learn import Lesson

        builder = self._builder(tmp_path)
        memory = builder.get_memory_for(None)
        memory._vector_store = SimpleNamespace(
            get_episodic_context=lambda query_text, cap: "",
            get_semantic_context=lambda query_text, cap, facts_only=False: "",
            get_preferences_context=lambda query_text="", cap=0: "",
            get_lessons_context=lambda query_text, cap, project_dir=None, recall_query=None, background=False, hard_cap=0, directive_budget=0, experience_budget=0: "",
            startup_lesson_query=lambda query_text: None,
            has_any_lesson=lambda: False,
        )
        assert builder.lessons.save(Lesson(ts="t", rule=self.RULE, category="tool")) == "inserted"
        assert (
            builder.lessons.save(
                Lesson(
                    ts="t",
                    rule="check streaming compatibility before release",
                    category="preference",
                    negative=self.NEGATIVE,
                )
            )
            == "inserted"
        )
        for rule, category in self.TOOLING_RULES:
            assert builder.lessons.save(Lesson(ts="t", rule=rule, category=category)) == "inserted"

        message, _ = builder.build_message("check wheel compatibility", True, "s-version-jsonl")

        assert self.RULE in message
        assert self.NEGATIVE in message
        for rule, _category in self.TOOLING_RULES:
            assert rule in message

    def test_vector_context_renders_durable_model_version_references(self, tmp_path):
        from kiro_crew.vector_memory import LessonWriteOutcome, VectorMemoryStore

        builder = self._builder(tmp_path)
        memory = builder.get_memory_for(None)
        vector_store = VectorMemoryStore(db_path=tmp_path / "vectors.db", embedding_dim=4)
        vector_store.init()
        try:
            memory._vector_store = vector_store
            first = vector_store.write_lesson(self.RULE, "tool")
            second = vector_store.write_lesson(
                "check streaming compatibility before release",
                "preference",
                negative=self.NEGATIVE,
            )

            assert first.outcome is LessonWriteOutcome.INSERTED
            assert second.outcome is LessonWriteOutcome.INSERTED
            assert vector_store.has_any_lesson() is True
            rendered = vector_store.get_lessons_context("wheel compatibility")
            assert self.RULE in rendered
            assert self.NEGATIVE in rendered

            message, _ = builder.build_message(
                "check wheel compatibility",
                True,
                "s-version-vector",
            )
            assert self.RULE in message
            assert self.NEGATIVE in message
        finally:
            vector_store.close()

    @pytest.mark.parametrize("rule,category", TOOLING_RULES)
    def test_vector_context_renders_model_qualified_tooling(
        self, tmp_path, rule: str, category: str
    ) -> None:
        from kiro_crew.vector_memory import LessonWriteOutcome, VectorMemoryStore

        builder = self._builder(tmp_path)
        memory = builder.get_memory_for(None)
        vector_store = VectorMemoryStore(db_path=tmp_path / "tooling.db", embedding_dim=4)
        vector_store.init()
        try:
            memory._vector_store = vector_store
            result = vector_store.write_lesson(rule, category)

            assert result.outcome is LessonWriteOutcome.INSERTED
            assert vector_store.has_any_lesson() is True
            assert rule in vector_store.get_lessons_context(rule)

            message, _ = builder.build_message(rule, True, "s-version-tooling")
            assert rule in message
        finally:
            vector_store.close()

    def test_key_confirmed_legacy_negative_pin_yields_jsonl_fallback(self, tmp_path):
        from kiro_crew.learn import Lesson
        from kiro_crew.vector_memory import (
            _LESSON_NEGATIVE_SEP,
            VectorMemoryStore,
            _lesson_key,
        )

        builder = self._builder(tmp_path)
        memory = builder.get_memory_for(None)
        vector_store = VectorMemoryStore(db_path=tmp_path / "legacy-vectors.db", embedding_dim=4)
        vector_store.init()
        rule = "automatic selection is safest"
        try:
            memory._vector_store = vector_store
            vector_store.set_semantic(
                _lesson_key(rule),
                f"{rule}{_LESSON_NEGATIVE_SEP}Select gpt-5.6-sol for reviews",
                1.0,
                "user_explicit",
            )
            assert vector_store.has_any_lesson() is False
            assert vector_store.get_lessons_context() == ""
            assert (
                builder.lessons.save(Lesson(ts="t", rule="JSONL-SENTINEL", category="tool"))
                == "inserted"
            )

            message, _ = builder.build_message(
                "check fallback",
                True,
                "s-legacy-negative-fallback",
            )

            assert "JSONL-SENTINEL" in message
            assert "gpt-5.6-sol" not in message
        finally:
            vector_store.close()
