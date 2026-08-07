"""Tests for context builder."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from kiro_crew.context import ContextBuilder
from kiro_crew.hooks import ContextRule, HookManager, HooksConfig
from kiro_crew.learn import LessonStore
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Valid workspace/memory_store names: non-empty alphanumeric + hyphens/underscores
_name_st = st.text(
    alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789-_"),
    min_size=1,
    max_size=30,
)


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------


class TestMemoryStoreOverrideProperty:
    # Feature: multi-agent-orchestration, Property 7: Memory store parameter overrides workspace for memory lookup
    @given(workspace=_name_st, memory_store=_name_st)
    @settings(deadline=None)
    def test_memory_store_overrides_workspace_in_build_session_context(
        self, workspace: str, memory_store: str, tmp_path_factory
    ):
        """**Validates: Requirements 3.1, 3.2, 3.3**

        When build_session_context is called with both a workspace and a
        distinct memory_store parameter, get_memory_for must be called
        with the memory_store value, not the workspace value.
        """
        tmp = tmp_path_factory.mktemp("ws")
        builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp / "ws"),
            skills=SkillsLoader(skills_path=tmp / "skills", install_builtins=False),
        )

        calls: list[str | None] = []
        original_get_memory = ContextBuilder.get_memory_for

        def _tracking_get_memory(key=None):
            calls.append(key)
            return original_get_memory(key)

        with patch.object(ContextBuilder, "get_memory_for", side_effect=_tracking_get_memory):
            builder.build_session_context(
                workspace=workspace,
                memory_store=memory_store,
            )

        # get_memory_for should have been called with memory_store, not workspace
        assert any(
            c == memory_store for c in calls
        ), f"Expected get_memory_for to be called with {memory_store!r}, got calls: {calls}"
        # When memory_store differs from workspace, workspace should NOT appear
        if memory_store != workspace:
            assert not any(
                c == workspace for c in calls
            ), f"get_memory_for should NOT be called with workspace {workspace!r} when memory_store={memory_store!r}"


class TestContextBuilder:
    def test_empty_context_has_critical_rules(self, tmp_path):
        builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
            lessons=LessonStore(base_dir=tmp_path),
        )
        ctx = builder.build_session_context()
        assert "[CRITICAL RULES" in ctx
        assert "diff" in ctx
        # ACP agents get the OPTIONS-button UI contract too
        assert "[OPTIONS:" in ctx
        # ...and the standalone-final-message rule (decision context must not
        # live only in a now-collapsed step)
        assert "collapses earlier steps" in ctx
        # ...and the option-label voice rule. Labels are sent verbatim as the
        # user's next message, so agent-voice labels ("I'll merge it") read
        # backwards once clicked.
        assert "in the USER's voice" in ctx

    def test_cc_provider_has_full_parity_with_kiro(self, tmp_path):
        """Full parity: anything injected for kiro ACP must also be injected for
        the Claude Code provider. The original bug — CC's clickable input-box
        options never rendered — was caused by CC being steered to the
        AskUserQuestion tool and skipping _CRITICAL_RULES, so the [OPTIONS: ...]
        tag (the only thing the dashboard/Slack UI renders) was never emitted.
        CC must get the SAME critical rules as kiro.
        """
        builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
            lessons=LessonStore(base_dir=tmp_path),
        )
        cc_ctx = builder.build_session_context(provider_type="claude_code")
        acp_ctx = builder.build_session_context(provider_type="acp")
        # CC gets the SAME critical-rules block as kiro (OPTIONS, diff, paths)
        assert "[CRITICAL RULES" in cc_ctx, "CC missing critical rules"
        assert "[OPTIONS:" in cc_ctx, "CC missing OPTIONS-button instruction"
        assert "diff" in cc_ctx, "CC missing diff-block instruction"
        assert "absolute path" in cc_ctx, "CC missing absolute-path file-link rule"
        assert "collapses earlier steps" in cc_ctx, "CC missing standalone-final-message rule"
        # Parity: both providers carry the critical-rules block.
        assert ("[CRITICAL RULES" in cc_ctx) == ("[CRITICAL RULES" in acp_ctx)

    def test_cc_interactive_reminder_uses_options_tag(self, tmp_path):
        """The interactive-choices reminder must tell CC to use [OPTIONS: ...]
        (the rendered tag), NOT the AskUserQuestion tool (which the UI does not
        render as clickable input-box options)."""
        builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
            lessons=LessonStore(base_dir=tmp_path),
        )
        msg, _ = builder.build_message(
            "pick one",
            is_new_session=False,
            interactive=True,
            provider_type="claude_code",
        )
        assert "[OPTIONS:" in msg, "CC interactive reminder must use the [OPTIONS:] tag"
        assert "AskUserQuestion" not in msg, "CC must not be steered to AskUserQuestion for options"

    def test_dashboard_tool_nudges_only_in_dashboard_sessions(self, tmp_path):
        """Card-tool nudges appear only where their dashboard surfaces exist;
        Slack/cron/subagent contexts must not be prompted to call either tool."""
        builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
            lessons=LessonStore(base_dir=tmp_path),
        )
        dash, _ = builder.build_message(
            "done", is_new_session=False, interactive=True, session_key="dashboard:chat-1"
        )
        assert "ask_question" in dash, "dashboard session must get the question nudge"
        assert "BEFORE" in dash and "ENDING" in dash
        assert "suggest_followup" in dash, "dashboard session must get the follow-up nudge"

        for sk in (None, "cron:job-1", "subagent:abc", "slack:C123"):
            other, _ = builder.build_message(
                "done", is_new_session=False, interactive=True, session_key=sk
            )
            assert "ask_question" not in other, f"{sk!r} must NOT get the question nudge"
            assert "suggest_followup" not in other, f"{sk!r} must NOT get the follow-up nudge"

    def test_dashboard_tool_nudges_require_interactive(self, tmp_path):
        """A non-interactive turn (e.g. automation) gets neither the OPTIONS
        reminder nor either dashboard-card tool nudge."""
        builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
            lessons=LessonStore(base_dir=tmp_path),
        )
        msg, _ = builder.build_message(
            "done", is_new_session=False, interactive=False, session_key="dashboard:chat-1"
        )
        assert "ask_question" not in msg
        assert "suggest_followup" not in msg

    def test_memory_injected(self, tmp_path):
        ws = tmp_path / "ws"
        store = MemoryStore(workspace=ws)
        store.write("# Memory\n\nUser likes Python.")
        builder = ContextBuilder(
            memory=store,
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )
        ctx = builder.build_session_context()
        assert "Python" in ctx
        assert "[Memory" in ctx

    def test_skills_injected(self, tmp_path):
        skills_dir = tmp_path / "skills" / "test"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text(
            "---\nname: test\ndescription: Test\nalways: true\n---\n# Test\nDo stuff."
        )
        builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )
        ctx = builder.build_session_context()
        assert "[Skills:]" in ctx
        assert "Do stuff." in ctx

    def _reinject_builder(self, tmp_path):
        """Builder with one on-demand skill, so the index has real content."""
        skills_dir = tmp_path / "skills" / "widget-maker"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text(
            "---\nname: widget-maker\ndescription: Build a widget.\n---\n# WidgetMaker\nBody.",
            encoding="utf-8",
        )
        return ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )

    def test_reinjection_adds_the_skills_index_after_compaction(self, tmp_path):
        """With the flag set on a continuing session, the index comes back
        wrapped in the marker so the model can still discover skills."""
        builder = self._reinject_builder(tmp_path)
        msg, _ = builder.build_message(
            "carry on", is_new_session=False, needs_reinjection=True
        )
        assert "[REINJECTED AFTER COMPACTION" in msg
        assert "[END REINJECTED]" in msg
        assert "widget-maker" in msg, "the re-injected block must carry the skill index"

    def test_no_reinjection_when_the_flag_is_absent(self, tmp_path):
        """The default path is unchanged — no marker, no index re-injection."""
        builder = self._reinject_builder(tmp_path)
        msg, _ = builder.build_message("carry on", is_new_session=False)
        assert "[REINJECTED AFTER COMPACTION" not in msg

    def test_no_reinjection_on_a_new_session(self, tmp_path):
        """A new session already gets the index from the session context;
        re-injecting would duplicate it in the same prompt."""
        builder = self._reinject_builder(tmp_path)
        msg, _ = builder.build_message(
            "first turn", is_new_session=True, needs_reinjection=True
        )
        assert "[REINJECTED AFTER COMPACTION" not in msg

    def test_no_reinjection_for_an_unmapped_custom_agent(self, tmp_path):
        """Mirrors the session-start gate (`inject_skills = ... not is_custom`).

        A custom agent's session-start context deliberately carries no skills
        block, so re-injecting one would ADD context rather than restore what
        compaction dropped.
        """
        builder = self._reinject_builder(tmp_path)
        msg, _ = builder.build_message(
            "carry on",
            is_new_session=False,
            needs_reinjection=True,
            agent="some-custom-agent",
        )
        assert "[REINJECTED AFTER COMPACTION" not in msg
        assert "widget-maker" not in msg

    def test_reinjection_still_fires_for_the_default_agent(self, tmp_path):
        """The gate must not over-block: the unmapped default agent is exactly
        the case the re-injection exists for."""
        builder = self._reinject_builder(tmp_path)
        msg, _ = builder.build_message(
            "carry on",
            is_new_session=False,
            needs_reinjection=True,
            agent="kirocrew",
        )
        assert "[REINJECTED AFTER COMPACTION" in msg
        assert "widget-maker" in msg

    def test_build_message_new_session(self, tmp_path):
        ws = tmp_path / "ws"
        store = MemoryStore(workspace=ws)
        store.write("# Memory\n\nUser likes lobsters.")
        builder = ContextBuilder(
            memory=store,
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )
        msg, hook = builder.build_message("hello", is_new_session=True)
        assert "lobsters" in msg
        assert "hello" in msg

    def test_build_message_injects_folder_breadcrumb(self, tmp_path):
        builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )
        # build_message injects whenever the caller supplies folder_path
        # (is_new_session=False here proves the block is not gated to new sessions).
        msg, _ = builder.build_message(
            "hello", is_new_session=False, folder_path="KiroCrew › Backend"
        )
        assert "[FOLDER]" in msg
        assert "KiroCrew › Backend" in msg
        # Absent when no folder path is supplied.
        msg_none, _ = builder.build_message("hello", is_new_session=False)
        assert "[FOLDER]" not in msg_none

    def test_build_message_existing_session(self, tmp_path):
        ws = tmp_path / "ws"
        store = MemoryStore(workspace=ws)
        store.write("# Memory\n\nUser likes lobsters.")
        builder = ContextBuilder(
            memory=store,
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )
        msg, hook = builder.build_message("hello", is_new_session=False)
        # No memory context on subsequent messages
        assert "lobsters" not in msg
        assert msg.startswith("hello")

    def test_hook_inject_context(self, tmp_path):
        hooks_cfg = HooksConfig(
            context_rules=[ContextRule(triggers=["pipeline"], context="Use pipeline tool.")]
        )
        builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
            hooks=HookManager(hooks_cfg),
        )
        msg, hook = builder.build_message("check pipeline", is_new_session=False)
        assert "[Hook context:]" in msg
        assert "pipeline tool" in msg

    def test_hook_modify(self, tmp_path):
        from kiro_crew.hooks import TransformHook

        hooks_cfg = HooksConfig(transforms=[TransformHook(pattern="deploy", prefix="[DEPLOY]")])
        builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
            hooks=HookManager(hooks_cfg),
        )
        msg, hook = builder.build_message("deploy app", is_new_session=False)
        assert msg.startswith("[DEPLOY]")

    def test_dashboard_cross_session_removed(self, tmp_path):
        """Cross-tab context injection is removed -- sibling sessions never leak."""
        from kiro_crew.history import ConversationLog

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        conv_log.append("dashboard:chat-1-100", "user", "what is 2+2?")
        conv_log.append("dashboard:chat-1-100", "assistant", "4")

        builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
            conversation_log=conv_log,
        )
        ctx = builder.build_session_context("dashboard:chat-2-200")
        assert "Other chat tabs" not in ctx
        assert "what is 2+2?" not in ctx

    def test_history_budget_truncates_long_messages(self, tmp_path):
        """Long assistant messages are truncated to _PER_MESSAGE_CAP."""
        from kiro_crew.history import ConversationLog

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        conv_log.append("dashboard:tab-1", "user", "show me the code")
        conv_log.append("dashboard:tab-1", "assistant", "x" * 10000)

        builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
            conversation_log=conv_log,
        )
        ctx = builder.build_session_context("dashboard:tab-1")
        assert "…[truncated]" in ctx
        # Full 10000-char message should NOT appear
        assert "x" * 10000 not in ctx

    def test_history_budget_limits_total_chars(self, tmp_path):
        """History injection respects _HISTORY_BUDGET_CHARS."""
        from kiro_crew.context import _HISTORY_BUDGET_CHARS
        from kiro_crew.history import ConversationLog

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        # Add many messages that together exceed the budget
        for i in range(40):
            conv_log.append("thread-1", "user", f"question {i} " + "z" * 200)
            conv_log.append("thread-1", "assistant", f"answer {i} " + "z" * 200)

        builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
            conversation_log=conv_log,
        )
        ctx = builder.build_session_context("thread-1")
        # History portion should be bounded
        history_start = ctx.find("[THREAD CONVERSATION HISTORY")
        history_end = ctx.find("[End of thread history]")
        assert history_start >= 0
        history_block = ctx[history_start:history_end]
        assert len(history_block) <= _HISTORY_BUDGET_CHARS + 1000  # some overhead for labels


class TestGetMemoryForVectorStore:
    """Tests for symmetric vector_store attachment across memory stores."""

    def test_nondefault_store_shares_vector_store(self, tmp_path, monkeypatch):
        """Non-default stores get the same vector_store as the default store."""
        import kiro_crew.context as ctx_mod

        original = ctx_mod._memory_stores.copy()
        ctx_mod._memory_stores.clear()
        monkeypatch.setattr(ctx_mod, "workspace_dir_for", lambda key: tmp_path / key)
        try:
            default_store = MemoryStore(workspace=tmp_path / "default")
            default_store.init()
            mock_vs = object()  # sentinel
            default_store.vector_store = mock_vs
            ctx_mod._memory_stores["default"] = default_store

            result = ContextBuilder.get_memory_for("custom-agent")

            assert result.vector_store is mock_vs
            assert result is not default_store
            assert result._workspace != default_store._workspace
        finally:
            ctx_mod._memory_stores.clear()
            ctx_mod._memory_stores.update(original)

    def test_nondefault_store_without_default_has_no_vector_store(self, tmp_path, monkeypatch):
        """If no default store exists yet, non-default store gets no vector_store."""
        import kiro_crew.context as ctx_mod

        original = ctx_mod._memory_stores.copy()
        ctx_mod._memory_stores.clear()
        monkeypatch.setattr(ctx_mod, "workspace_dir_for", lambda key: tmp_path / key)
        try:
            result = ContextBuilder.get_memory_for("orphan")
            assert result.vector_store is None
        finally:
            ctx_mod._memory_stores.clear()
            ctx_mod._memory_stores.update(original)


class TestCompressAssistantMessage:
    """Tests for _compress_assistant_message code block and JSON handling."""

    def test_small_code_block_preserved(self):
        from kiro_crew.context import _compress_assistant_message

        text = "Here:\n```python\nprint('hi')\n```\nDone."
        assert _compress_assistant_message(text) == text

    def test_large_code_block_head_tail(self):
        from kiro_crew.context import _compress_assistant_message

        lines = [f"line {i} " + "a" * 100 for i in range(30)]
        block = "```python\n" + "\n".join(lines) + "\n```"
        result = _compress_assistant_message(f"Before\n{block}\nAfter")
        assert "line 0" in result
        assert "line 9" in result  # head: first 10
        assert "line 25" in result  # tail: last 5
        assert "15 lines omitted" in result
        assert "line 15" not in result  # middle omitted

    def test_few_long_lines_char_truncated(self):
        from kiro_crew.context import _compress_assistant_message

        # 5 lines of 1K each = 5K total, >2K but <=15 lines
        lines = ["x" * 1000 for _ in range(5)]
        block = "```python\n" + "\n".join(lines) + "\n```"
        result = _compress_assistant_message(block)
        assert "chars truncated" in result
        assert len(result) < len(block)

    def test_json_blob_small_preserved(self):
        from kiro_crew.context import _compress_assistant_message

        text = 'Result: {"key": "value", "num": 42}'
        assert _compress_assistant_message(text) == text

    def test_json_blob_large_truncated(self):
        from kiro_crew.context import _compress_assistant_message

        blob = '{"data": "' + "x" * 1500 + '"}'
        result = _compress_assistant_message(f"Output: {blob}")
        assert "[tool output truncated]" in result


class TestDocsSection:
    def test_docs_section_present_when_docs_exist(self, tmp_path, monkeypatch):
        """_build_docs_section returns content when docs dir exists."""
        from kiro_crew import context as ctx_mod

        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()
        (docs_dir / "getting-started.md").write_text("# Getting Started\n")
        monkeypatch.setattr(ctx_mod, "_BUNDLED_DOCS_DIR", docs_dir)

        result = ctx_mod._build_docs_section()
        assert "[DOCUMENTATION]" in result
        assert str(docs_dir) in result
        assert "consult local docs first" in result

    def test_docs_section_empty_when_no_docs(self, tmp_path, monkeypatch):
        """_build_docs_section returns empty string when docs dir missing."""
        from kiro_crew import context as ctx_mod

        monkeypatch.setattr(ctx_mod, "_BUNDLED_DOCS_DIR", tmp_path / "nonexistent")

        result = ctx_mod._build_docs_section()
        assert result == ""

    def test_docs_injected_for_kirocrew_agent(self, tmp_path, monkeypatch):
        """build_session_context includes docs for the default kirocrew agent."""
        from kiro_crew import context as ctx_mod

        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()
        (docs_dir / "getting-started.md").write_text("# Getting Started\n")
        monkeypatch.setattr(ctx_mod, "_BUNDLED_DOCS_DIR", docs_dir)

        builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )
        ctx = builder.build_session_context()
        assert "[DOCUMENTATION]" in ctx

    def test_docs_not_injected_for_custom_agent(self, tmp_path, monkeypatch):
        """build_session_context skips docs for custom (non-kirocrew) agents."""
        from kiro_crew import context as ctx_mod

        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()
        (docs_dir / "getting-started.md").write_text("# Getting Started\n")
        monkeypatch.setattr(ctx_mod, "_BUNDLED_DOCS_DIR", docs_dir)

        builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )
        ctx = builder.build_session_context(agent="code-reviewer")
        assert "[DOCUMENTATION]" not in ctx


class TestCompressThreadHistory:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_history(self, tmp_path):
        from kiro_crew.context import compress_thread_history
        from kiro_crew.history import ConversationLog

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        sessions = object()  # unused — no messages to compress
        result = await compress_thread_history(conv_log, "no-thread", "hi", sessions)
        assert result is None

    @pytest.mark.asyncio
    async def test_short_transcript_returned_without_llm(self, tmp_path):
        from kiro_crew.context import compress_thread_history
        from kiro_crew.history import ConversationLog

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        conv_log.append("t1", "user", "hello")
        conv_log.append("t1", "assistant", "hi there")
        sessions = object()  # unused — transcript is short
        result = await compress_thread_history(conv_log, "t1", "hello", sessions)
        assert result is not None
        assert "hello" in result
        assert "hi there" in result

    @pytest.mark.asyncio
    async def test_long_transcript_calls_llm(self, tmp_path, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.context import compress_thread_history
        from kiro_crew.history import ConversationLog

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        for i in range(50):
            conv_log.append("t1", "user", f"msg {i} " + "x" * 1400)
            conv_log.append("t1", "assistant", f"reply {i} " + "y" * 1400)

        mock_client = MagicMock()
        mock_sessions = MagicMock()
        mock_sessions.get_pid = MagicMock(return_value=None)
        mock_sessions.get_or_create = AsyncMock(return_value=(mock_client, True, False))
        mock_sessions.release = MagicMock()
        mock_sessions.recycle_background = AsyncMock()

        monkeypatch.setattr(
            "kiro_crew.llm_helpers.stream_and_collect",
            AsyncMock(return_value="compressed summary here"),
        )

        result = await compress_thread_history(conv_log, "t1", "latest q", mock_sessions)
        assert result is not None
        assert "compressed summary here" in result
        assert "Thread start (verbatim)" in result
        assert "Compressed history" in result
        assert "Recent exchanges (verbatim)" in result
        mock_sessions.release.assert_called_once()
        mock_sessions.recycle_background.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_llm_failure_returns_none(self, tmp_path, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.context import compress_thread_history
        from kiro_crew.history import ConversationLog

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        for i in range(50):
            conv_log.append("t1", "user", f"msg {i} " + "x" * 1400)
            conv_log.append("t1", "assistant", f"reply {i} " + "y" * 1400)

        mock_sessions = MagicMock()
        mock_sessions.get_pid = MagicMock(return_value=None)
        mock_sessions.get_or_create = AsyncMock(side_effect=RuntimeError("boom"))
        mock_sessions.release = MagicMock()
        mock_sessions.recycle_background = AsyncMock()

        result = await compress_thread_history(conv_log, "t1", "q", mock_sessions)
        assert result is None
        mock_sessions.release.assert_not_called()
        mock_sessions.recycle_background.assert_not_awaited()

    def test_build_session_context_uses_compressed_history(self, tmp_path):
        """When compressed_history is passed, it replaces naive truncation."""
        from kiro_crew.history import ConversationLog

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        conv_log.append("t1", "user", "what color?")
        conv_log.append("t1", "assistant", "blue")

        builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
            conversation_log=conv_log,
        )
        ctx = builder.build_session_context(
            "t1", compressed_history="COMPRESSED: user asked about color, answer was blue"
        )
        assert "COMPRESSED: user asked about color" in ctx

    @pytest.mark.asyncio
    async def test_compressed_output_redacts_credentials(self, tmp_path, monkeypatch):
        """Credentials in LLM compression output must be scrubbed."""
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.context import compress_thread_history
        from kiro_crew.history import ConversationLog

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        for i in range(50):
            conv_log.append("t1", "user", f"msg {i} " + "x" * 500)
            conv_log.append("t1", "assistant", f"reply {i} " + "y" * 500)

        mock_sessions = MagicMock()
        mock_sessions.get_pid = MagicMock(return_value=None)
        mock_sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
        mock_sessions.release = MagicMock()
        mock_sessions.recycle_background = AsyncMock()

        fake_key = "AKIAIOSFODNN7EXAMPLE"
        monkeypatch.setattr(
            "kiro_crew.llm_helpers.stream_and_collect",
            AsyncMock(return_value=f"summary with {fake_key} leaked"),
        )

        result = await compress_thread_history(conv_log, "t1", "q", mock_sessions)
        assert result is not None
        assert fake_key not in result


class TestLoadAgentPrompt:
    """Tests for _load_agent_prompt handling of null/missing prompt values."""

    def test_null_prompt_returns_empty(self, tmp_path, monkeypatch):
        """Agent JSON with "prompt": null should return empty string."""
        import json

        agents_dir = tmp_path / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "test.json").write_text(
            json.dumps({"name": "test", "prompt": None}), encoding="utf-8"
        )
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        assert ContextBuilder._load_agent_prompt("test") == ""

    def test_missing_prompt_returns_empty(self, tmp_path, monkeypatch):
        """Agent JSON without "prompt" key should return empty string."""
        import json

        agents_dir = tmp_path / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "test.json").write_text(json.dumps({"name": "test"}), encoding="utf-8")
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        assert ContextBuilder._load_agent_prompt("test") == ""


class TestRuntimeDisplayName:
    """Tests for _runtime_display_name() and agent identity injection."""

    @pytest.mark.parametrize(
        "session_key, expected_runtime",
        [
            ("dashboard:chat-1-100", "KiroCrew dashboard"),
            ("dashboard_chat-1-100", "KiroCrew dashboard"),
            ("cron:daily", "KiroCrew cron job"),
            ("cron_076ab486", "KiroCrew cron job"),
            ("subagent:abc-123", "KiroCrew subagent"),
            ("taskrunner:proj:task1", "KiroCrew task runner"),
            ("_bg", "KiroCrew background"),
            ("_hb", "KiroCrew heartbeat"),
            ("cli_chat", "CLI terminal"),
            ("slack:1234567890.123456", "Slack"),
            ("discord:kirocrew:direct:474737235959480320", "Discord"),
            ("discord_kirocrew_direct_474737235959480320", "Discord"),
            ("telegram:kirocrew:direct:123", "Telegram"),
            ("wecom:kirocrew:direct:user@example.com", "WeCom"),
            ("weixin:kirocrew:direct:wxid", "Weixin"),
            ("webex:kirocrew:direct:user@example.com", "Webex"),
            ("teams:kirocrew:direct:user@example.com", "Microsoft Teams"),
            ("1234567890.123456", "Slack"),
        ],
    )
    def test_runtime_display_name(self, session_key, expected_runtime):
        from kiro_crew.context import _runtime_display_name

        assert _runtime_display_name(session_key) == expected_runtime

    def test_agent_identity_injected_with_session_key(self, tmp_path):
        """build_session_context injects [CURRENT AGENT] and [RUNTIME] when session_key is provided."""
        builder = ContextBuilder(memory=MemoryStore(workspace=tmp_path))
        ctx = builder.build_session_context("dashboard:chat-1", agent="gpu-comms")
        assert "[CURRENT AGENT] gpu-comms" in ctx
        assert "[RUNTIME] KiroCrew dashboard" in ctx

    def test_agent_identity_omitted_without_session_key(self, tmp_path):
        """build_session_context omits agent identity when session_key is None."""
        builder = ContextBuilder(memory=MemoryStore(workspace=tmp_path))
        ctx = builder.build_session_context()
        assert "[CURRENT AGENT]" not in ctx
        assert "[RUNTIME]" not in ctx

    def test_agent_defaults_to_kirocrew(self, tmp_path):
        """Agent label defaults to 'kirocrew' when agent param is None."""
        builder = ContextBuilder(memory=MemoryStore(workspace=tmp_path))
        ctx = builder.build_session_context("dashboard:chat-1")
        assert "[CURRENT AGENT] kirocrew" in ctx

    def test_explicit_runtime_source_overrides_stable_session_key(self, tmp_path):
        """The current transport wins when a dashboard session resumes elsewhere."""
        builder = ContextBuilder(memory=MemoryStore(workspace=tmp_path))
        ctx = builder.build_session_context(
            "dashboard:chat-1",
            runtime_source="discord",
        )
        assert "[RUNTIME] Discord" in ctx
        assert "[RUNTIME] KiroCrew dashboard" not in ctx

    def test_follow_up_refreshes_runtime_from_current_transport(self, tmp_path):
        """Warm cross-surface sessions receive authoritative per-turn runtime."""
        builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )
        msg, _ = builder.build_message(
            "where am I talking to you?",
            is_new_session=False,
            session_key="dashboard:chat-1",
            runtime_source="discord",
        )
        assert "[RUNTIME] Discord" in msg
        assert "authoritative for this turn" in msg
        assert msg.index("[RUNTIME] Discord") < msg.index("[CURRENT USER REQUEST")


class TestMultibyteSanitization:
    """Tests for multi-byte UTF-8 sanitization (kiro-cli panic workaround)."""

    def test_build_message_strips_multibyte(self, tmp_path):
        """build_message replaces multi-byte punctuation with ASCII equivalents."""
        builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )
        msg, _ = builder.build_message(
            "Check the pipeline \u2014 it\u2019s failing\u2026",
            is_new_session=False,
        )
        assert "\u2014" not in msg
        assert "\u2019" not in msg
        assert "\u2026" not in msg
        assert "--" in msg
        assert "'" in msg
        assert "..." in msg

    def test_build_message_new_session_strips_multibyte(self, tmp_path):
        """Multi-byte chars in memory/skills context are also sanitized."""
        ws = tmp_path / "ws"
        store = MemoryStore(workspace=ws)
        store.write(
            "# Memory\n\nUser prefers \u201csmart quotes\u201d and em dashes \u2014 always."
        )
        builder = ContextBuilder(
            memory=store,
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )
        msg, _ = builder.build_message("hello", is_new_session=True)
        assert "\u201c" not in msg
        assert "\u201d" not in msg
        assert "\u2014" not in msg

    def test_multibyte_table_covers_all_chars(self):
        """Translation table handles all listed multi-byte chars."""
        from kiro_crew.context import _MULTIBYTE_TABLE

        sample = "\u2014 \u2013 \u2018 \u2019 \u201c \u201d \u2026 \u00a0 \u2022"
        result = sample.translate(_MULTIBYTE_TABLE)
        assert result == "-- - ' ' \" \" ...   -"

    @pytest.mark.asyncio
    async def test_compress_thread_history_strips_multibyte(self, tmp_path):
        """Short transcript with multi-byte chars gets sanitized."""
        from kiro_crew.context import compress_thread_history
        from kiro_crew.history import ConversationLog

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        conv_log.append("t1", "user", "what\u2019s the status \u2014 any update?")
        conv_log.append("t1", "assistant", "All good \u2026 no issues.")
        sessions = object()
        result = await compress_thread_history(conv_log, "t1", "hello", sessions)
        assert result is not None
        assert "\u2019" not in result
        assert "\u2014" not in result
        assert "\u2026" not in result


class TestCurrentDateTimezone:
    """[CURRENT DATE] injection must honour KiroCrewConfig.timezone, so LLMs
    see the user's local time rather than the gateway host TZ (often UTC)."""

    def _make_builder(self, tmp_path):
        return ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
            lessons=LessonStore(base_dir=tmp_path),
            hooks=HookManager(HooksConfig()),
        )

    def test_current_date_uses_configured_timezone(self, tmp_path):
        builder = self._make_builder(tmp_path)
        with patch("kiro_crew.cron.KiroCrewConfig.load") as mock_load:
            mock_load.return_value.timezone = "Asia/Tokyo"
            ctx = builder.build_session_context()
        # Tokyo is JST/UTC+9; %Z renders "JST"
        assert "[CURRENT DATE]" in ctx
        date_line = [ln for ln in ctx.splitlines() if ln.startswith("[CURRENT DATE]")][0]
        assert "JST" in date_line

    def test_current_date_falls_back_to_utc_when_config_empty(self, tmp_path):
        builder = self._make_builder(tmp_path)
        with patch("kiro_crew.cron.KiroCrewConfig.load") as mock_load:
            mock_load.return_value.timezone = ""
            ctx = builder.build_session_context()
        date_line = [ln for ln in ctx.splitlines() if ln.startswith("[CURRENT DATE]")][0]
        assert "UTC" in date_line


class TestLoadSteeringResources:
    """Tests for _load_steering_resources."""

    def test_loads_md_files_from_resources(self, tmp_path):
        from kiro_crew.context import _load_steering_resources

        # Create steering file
        steering_dir = tmp_path / ".kiro" / "steering"
        steering_dir.mkdir(parents=True)
        (steering_dir / "rules.md").write_text("# My Rules\nAlways be nice.")

        # Create agent config with resources
        agents_dir = tmp_path / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        import json

        (agents_dir / "kirocrew.json").write_text(
            json.dumps({"resources": ["file://.kiro/steering/**/*.md"]})
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _load_steering_resources()

        assert "My Rules" in result
        assert "Always be nice." in result

    def test_returns_empty_when_no_config(self, tmp_path):
        from kiro_crew.context import _load_steering_resources

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _load_steering_resources()

        assert result == ""

    def test_skips_sensitive_paths(self, tmp_path):
        from kiro_crew.context import _load_steering_resources

        # Create a .md file in a sensitive location
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        (ssh_dir / "keys.md").write_text("SECRET")

        agents_dir = tmp_path / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        import json

        (agents_dir / "kirocrew.json").write_text(json.dumps({"resources": ["file://.ssh/*.md"]}))

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _load_steering_resources()

        assert "SECRET" not in result

    def test_steering_injected_for_cc_but_not_acp(self, tmp_path):
        """kiro-cli loads an agent's ``resources`` natively when spawned with
        ``--agent`` (acp/client.py ``_spawn``), so build_session_context must
        NOT re-inject steering on the ACP backend — that would duplicate what
        kiro already loaded. The CC backend (claude-agent-acp) does not read
        agent ``resources``, so it still needs the explicit load.
        """
        import json

        steering_dir = tmp_path / ".kiro" / "steering"
        steering_dir.mkdir(parents=True)
        (steering_dir / "rules.md").write_text("# My Rules\nSTEERING_MARKER_XYZ")
        agents_dir = tmp_path / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "kirocrew.json").write_text(
            json.dumps({"resources": ["file://.kiro/steering/**/*.md"]})
        )

        builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
            lessons=LessonStore(base_dir=tmp_path),
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            cc_ctx = builder.build_session_context(provider_type="claude_code")
            acp_ctx = builder.build_session_context(provider_type="acp")

        assert "STEERING_MARKER_XYZ" in cc_ctx, "CC backend must get explicit steering load"
        assert "STEERING_MARKER_XYZ" not in acp_ctx, (
            "ACP backend must NOT re-inject steering — kiro-cli loads agent "
            "resources natively via --agent"
        )


class TestLessonsCap:
    def test_over_cap_injects_error_block(self, tmp_path):
        from kiro_crew.context import _LESSONS_CAP
        from kiro_crew.learn import Lesson

        lessons = LessonStore(base_dir=tmp_path)
        # Save enough long lessons that the formatted context exceeds the cap.
        rule = "x" * 1000
        for i in range(_LESSONS_CAP // 1000 + 5):
            lessons.save(Lesson(ts=str(i), rule=f"{i}-{rule}", category="knowledge"))

        builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "ws"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
            lessons=lessons,
        )
        ctx = builder.build_session_context()

        assert "CRITICAL ERROR — LESSONS FILE TOO LARGE" in ctx
        assert "remain in effect" in ctx
        assert "[lessons truncated]" in ctx
        assert "x" * 500 in ctx  # part of the kept lessons content is still present in ctx

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
        from pathlib import Path

        nested_scopes = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
        src_root = Path(__file__).resolve().parent.parent / "src" / "kiro_crew"
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

        for py in src_root.rglob("*.py"):
            try:
                text = py.read_text(encoding="utf-8")
                tree = ast.parse(text)
            except SyntaxError:
                continue
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
                    offenders.append(f"{py.relative_to(src_root)}:{call.lineno} in async {fn.name}")

        assert not offenders, (
            "build_message called inline from async coroutine(s) — the episodic "
            "query embed blocks the event loop; wrap in run_in_embed_pool (or "
            "add '# loop-ok: <reason>' if genuinely safe):\n  " + "\n  ".join(offenders)
        )
