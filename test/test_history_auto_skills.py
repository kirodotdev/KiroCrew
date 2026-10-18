"""Tests for the history consolidator's skill detection and auto-skills.

Covers the eligibility helpers (tool-call counts and the sensitive-session check,
in both message schemas), ``_process_auto_skills`` end to end, its SEL audit on
every no-write decision, the new-skill prompt's JSON shape and recurrence gate,
detection over the full session window, and candidate dedupe and script checks.
The consolidator's memory and lesson writes are in ``test_history_consolidator.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from unittest.mock import patch

import pytest

from kiro_crew import history
from kiro_crew.history import ConversationLog, HistoryConsolidator


class TestAutoSkillHelpers:
    """Module-level helpers for auto-skill eligibility."""

    def test_count_tool_call_messages(self):
        from kiro_crew.history import _count_tool_call_messages

        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello", "tools": ["fs_read"]},
            {"role": "user", "content": "do X"},
            {"role": "assistant", "content": "ok", "tools": ["fs_read", "execute_bash"]},
            {"role": "assistant", "content": "done", "tools": []},  # empty list counts as zero
            {"role": "assistant", "content": "another", "tools": ["fs_read"]},
        ]
        assert _count_tool_call_messages(messages) == 3

    def test_count_handles_malformed_tools(self):
        from kiro_crew.history import _count_tool_call_messages

        messages = [
            {"role": "assistant", "content": "x", "tools": "not-a-list"},
            {"role": "assistant", "content": "y"},
            {"role": "assistant", "content": "z", "tools": None},
        ]
        assert _count_tool_call_messages(messages) == 0

    def test_session_touched_sensitive_true_for_aws(self):
        from kiro_crew.history import _session_touched_sensitive

        messages = [
            {"role": "assistant", "content": "", "tools": ["Reading ~/.aws/credentials"]},
        ]
        assert _session_touched_sensitive(messages) is True

    def test_session_touched_sensitive_true_for_imds(self):
        from kiro_crew.history import _session_touched_sensitive

        messages = [
            {"role": "assistant", "content": "", "tools": ["curl 169.254.169.254/latest/..."]},
        ]
        assert _session_touched_sensitive(messages) is True

    def test_session_touched_sensitive_false_for_normal_tools(self):
        from kiro_crew.history import _session_touched_sensitive

        messages = [
            {"role": "assistant", "content": "", "tools": ["Running: ls /tmp", "fs_read"]},
            {"role": "assistant", "content": "", "tools": ["grep foo bar.txt"]},
        ]
        assert _session_touched_sensitive(messages) is False


class TestDashboardSchemaToolCallCounting:
    """Regression tests for dashboard-format tool messages (schema fix)."""

    def test_count_dashboard_role_tool_messages(self):
        """Dashboard pipeline records tool calls as role='tool' messages."""
        from kiro_crew.history import _count_tool_call_messages

        messages = [
            {"role": "user", "content": "find info on grading"},
            {"role": "assistant", "content": "Let me look that up."},
            {"role": "tool", "content": "🔧 Running: @builder-mcp/ReadInternalWebsites"},
            {"role": "tool", "content": "✅ Running: @builder-mcp/ReadInternalWebsites"},
            {"role": "assistant", "content": "Here's what I found."},
            {"role": "tool", "content": "🔧 Running: @builder-mcp/InternalCodeSearch"},
            {"role": "tool", "content": "✅ Running: @builder-mcp/InternalCodeSearch"},
        ]
        assert _count_tool_call_messages(messages) == 4

    def test_sensitive_detection_dashboard_schema(self):
        """Sensitive paths in dashboard tool content are detected."""
        from kiro_crew.history import _session_touched_sensitive

        messages = [
            {"role": "assistant", "content": "Reading credentials."},
            {"role": "tool", "content": "🔧 Running: read ~/.aws/credentials"},
            {"role": "tool", "content": "✅ Running: read ~/.aws/credentials"},
        ]
        assert _session_touched_sensitive(messages) is True

    def test_sensitive_false_for_normal_dashboard_tools(self):
        """Normal dashboard tool messages don't trigger sensitive detection."""
        from kiro_crew.history import _session_touched_sensitive

        messages = [
            {"role": "tool", "content": "🔧 Running: @builder-mcp/ReadInternalWebsites"},
            {"role": "tool", "content": "✅ Running: @builder-mcp/InternalCodeSearch"},
        ]
        assert _session_touched_sensitive(messages) is False

    def test_mixed_schema_no_double_count(self):
        """Sessions mixing legacy tools field and dashboard role='tool' count correctly."""
        from kiro_crew.history import _count_tool_call_messages

        messages = [
            {"role": "assistant", "content": "step 1", "tools": ["fs_read"]},
            {"role": "tool", "content": "🔧 Running: @builder-mcp/ReadInternalWebsites"},
            {"role": "tool", "content": "✅ Running: @builder-mcp/ReadInternalWebsites"},
            {"role": "assistant", "content": "step 2", "tools": ["grep"]},
            # Edge case: a message with BOTH signals (shouldn't happen but test no double-count)
            {"role": "tool", "content": "tool msg", "tools": ["fs_read"]},
        ]
        # 2 legacy + 2 dashboard-only + 1 that has both (counted once via legacy branch) = 5
        assert _count_tool_call_messages(messages) == 5


class TestProcessAutoSkillsIntegration:
    """End-to-end consolidator path with flag off and flag on (mocked LLM)."""

    @pytest.mark.asyncio
    async def test_consolidator_default_off_never_writes(self, tmp_path):
        """With auto_skills_enabled=False (default), no skill writes happen."""
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()
        skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)

        consolidator = HistoryConsolidator(
            log=conv_log,
            memory=mem,
            skills_loader=skills,
            auto_skills_enabled=False,
        )

        # Seed a session with 10 tool calls — would be eligible if flag were on
        for i in range(10):
            conv_log.append("dashboard:chat-1", "assistant", f"step {i}", tools=["fs_read"])

        async def fake_llm(_prompt, *, memory_store: str = "", session_key: str = ""):
            return {
                "history_entry": "did 10 things",
                "new_skill": {
                    "slug": "should-not-be-written",
                    "description": "test",
                    "triggers": "t1, t2",
                    "procedure_md": "body",
                },
            }

        with patch.object(consolidator, "_call_llm", side_effect=fake_llm):
            await consolidator._consolidate("dashboard:chat-1", include_history=True)

        # Flag off → no auto skill written
        assert skills.list_auto_skills() == []

    @pytest.mark.asyncio
    async def test_consolidator_on_creates_auto_skill(self, tmp_path):
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()
        skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)

        consolidator = HistoryConsolidator(
            log=conv_log,
            memory=mem,
            skills_loader=skills,
            auto_skills_enabled=True,
            approval_required=False,
            auto_min_tool_calls=5,
        )

        # 6 tool-call messages — above threshold, no sensitive paths
        for i in range(6):
            conv_log.append(
                "dashboard:chat-2", "assistant", f"step {i}", tools=["Running: grep foo bar.txt"]
            )

        async def fake_llm(_prompt, *, memory_store: str = "", session_key: str = ""):
            return {
                "history_entry": "did 6 things",
                "new_skill": {
                    "slug": "grep-with-context",
                    "description": "Search log files with grep then contextualize hits",
                    "triggers": "grep, log search, context lines",
                    "procedure_md": "## Steps\n1. grep -n pattern file\n2. Read ±5 lines\n",
                },
            }

        with patch.object(consolidator, "_call_llm", side_effect=fake_llm):
            await consolidator._consolidate("dashboard:chat-2", include_history=True)

        auto = skills.list_auto_skills()
        assert len(auto) == 1
        assert auto[0]["key"] == "auto/grep-with-context"
        skill_file = tmp_path / "skills" / "auto" / "grep-with-context" / "SKILL.md"
        assert skill_file.exists()
        content = skill_file.read_text(encoding="utf-8")
        assert "source: auto" in content
        assert "session_key: dashboard:chat-2" in content
        assert "grep -n pattern file" in content

    @pytest.mark.asyncio
    async def test_sensitive_session_skipped_even_when_enabled(self, tmp_path):
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()
        skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)

        consolidator = HistoryConsolidator(
            log=conv_log,
            memory=mem,
            skills_loader=skills,
            auto_skills_enabled=True,
            approval_required=False,
            auto_min_tool_calls=2,  # low threshold to force eligibility otherwise
        )

        for i in range(5):
            conv_log.append(
                "dashboard:chat-3",
                "assistant",
                f"step {i}",
                tools=["Reading ~/.aws/credentials"],
            )

        llm_called = False

        async def fake_llm(_prompt, *, memory_store: str = "", session_key: str = ""):
            nonlocal llm_called
            llm_called = True
            # The prompt built for this session should NOT include new_skill
            # because eligibility check failed.  Return basic keys only.
            return {"history_entry": "sensitive session"}

        with patch.object(consolidator, "_call_llm", side_effect=fake_llm):
            await consolidator._consolidate("dashboard:chat-3", include_history=True)

        assert llm_called  # consolidation still happened for memory
        # But no auto skill written
        assert skills.list_auto_skills() == []

    @pytest.mark.asyncio
    async def test_credentials_in_llm_output_are_redacted_before_write(self, tmp_path):
        """If the LLM returns a procedure with an AWS key, it's redacted before disk write."""
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()
        skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)

        consolidator = HistoryConsolidator(
            log=conv_log,
            memory=mem,
            skills_loader=skills,
            auto_skills_enabled=True,
            approval_required=False,
            auto_min_tool_calls=2,
        )

        for i in range(5):
            conv_log.append("dashboard:chat-4", "assistant", f"step {i}", tools=["fs_read"])

        async def fake_llm(_prompt, *, memory_store: str = "", session_key: str = ""):
            return {
                "history_entry": "x",
                "new_skill": {
                    "slug": "poison-skill",
                    "description": "A procedure involving things",
                    "triggers": "thing, procedure",
                    "procedure_md": (
                        "## Steps\n"
                        "1. Use AKIAIOSFODNN7EXAMPLE as the key\n"
                        "2. Run `aws sts get-caller-identity`\n"
                    ),
                },
            }

        with patch.object(consolidator, "_call_llm", side_effect=fake_llm):
            await consolidator._consolidate("dashboard:chat-4", include_history=True)

        skill_file = tmp_path / "skills" / "auto" / "poison-skill" / "SKILL.md"
        assert skill_file.exists()
        content = skill_file.read_text(encoding="utf-8")
        # AKIA prefix must NOT survive to disk
        assert "AKIAIOSFODNN7EXAMPLE" not in content

    @pytest.mark.asyncio
    async def test_similarity_dedup_skips_near_duplicate(self, tmp_path):
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()
        skills_dir = tmp_path / "skills"
        # Pre-existing skill we'd duplicate
        (skills_dir / "existing").mkdir(parents=True)
        (skills_dir / "existing" / "SKILL.md").write_text(
            "---\nname: existing\ndescription: Search timber logs via ssh chained patterns\n---\n"
        )
        skills = SkillsLoader(skills_path=skills_dir, install_builtins=False)

        consolidator = HistoryConsolidator(
            log=conv_log,
            memory=mem,
            skills_loader=skills,
            auto_skills_enabled=True,
            approval_required=False,
            auto_min_tool_calls=2,
            auto_similarity_threshold=0.5,
        )

        for i in range(5):
            conv_log.append("dashboard:chat-5", "assistant", f"step {i}", tools=["fs_read"])

        async def fake_llm(_prompt, *, memory_store: str = "", session_key: str = ""):
            return {
                "history_entry": "x",
                "new_skill": {
                    "slug": "similar-timber-search",
                    # Near-duplicate description → should be deduped
                    "description": "Search timber logs via ssh chained patterns",
                    "triggers": "timber, log",
                    "procedure_md": "body",
                },
            }

        with patch.object(consolidator, "_call_llm", side_effect=fake_llm):
            await consolidator._consolidate("dashboard:chat-5", include_history=True)

        auto = skills.list_auto_skills()
        assert auto == []  # dedup prevented creation

    @pytest.mark.asyncio
    async def test_dashboard_schema_messages_trigger_auto_skill(self, tmp_path):
        """Dashboard-format role='tool' messages pass eligibility and produce a skill.

        This is the regression test that would have caught the schema mismatch bug.
        """
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()
        skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)

        consolidator = HistoryConsolidator(
            log=conv_log,
            memory=mem,
            skills_loader=skills,
            auto_skills_enabled=True,
            approval_required=False,
            auto_min_tool_calls=5,
        )

        # Seed with REAL dashboard-format messages (no "tools" field anywhere)
        conv_log.append("dashboard:chat-schema", "user", "find info on grading services")
        conv_log.append("dashboard:chat-schema", "assistant", "Let me look that up.")
        conv_log.append(
            "dashboard:chat-schema", "tool", "🔧 Running: @builder-mcp/ReadInternalWebsites"
        )
        conv_log.append(
            "dashboard:chat-schema", "tool", "✅ Running: @builder-mcp/ReadInternalWebsites"
        )
        conv_log.append("dashboard:chat-schema", "assistant", "Now checking sub-pages.")
        conv_log.append(
            "dashboard:chat-schema", "tool", "🔧 Running: @builder-mcp/ReadInternalWebsites"
        )
        conv_log.append(
            "dashboard:chat-schema", "tool", "✅ Running: @builder-mcp/ReadInternalWebsites"
        )
        conv_log.append(
            "dashboard:chat-schema", "tool", "🔧 Running: @builder-mcp/InternalCodeSearch"
        )
        conv_log.append(
            "dashboard:chat-schema", "tool", "✅ Running: @builder-mcp/InternalCodeSearch"
        )
        conv_log.append("dashboard:chat-schema", "assistant", "Here's the full list.")

        async def fake_llm(_prompt, *, memory_store: str = "", session_key: str = ""):
            return {
                "history_entry": "explored grading services",
                "new_skill": {
                    "slug": "dashboard-wiki-explorer",
                    "description": "Navigate wiki sub-pages to enumerate services",
                    "triggers": "wiki, services, enumerate",
                    "procedure_md": "## Steps\n1. Read root wiki page\n2. Follow sub-links\n",
                },
            }

        with patch.object(consolidator, "_call_llm", side_effect=fake_llm):
            await consolidator._consolidate("dashboard:chat-schema", include_history=True)

        auto = skills.list_auto_skills()
        assert len(auto) == 1
        assert auto[0]["key"] == "auto/dashboard-wiki-explorer"

    @pytest.mark.asyncio
    async def test_consolidator_stages_when_approval_required(self, tmp_path):
        """With approval_required (the default), a new skill goes to the pending
        queue — not live — and is audited with outcome='staged'."""
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()
        skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)

        consolidator = HistoryConsolidator(
            log=conv_log,
            memory=mem,
            skills_loader=skills,
            auto_skills_enabled=True,
            auto_min_tool_calls=5,
            # approval_required defaults True → staged, not live
        )

        for i in range(6):
            conv_log.append(
                "dashboard:chat-stage",
                "assistant",
                f"step {i}",
                tools=["Running: grep foo bar.txt"],
            )

        async def fake_llm(_prompt, *, memory_store: str = "", session_key: str = ""):
            return {
                "history_entry": "did staged things",
                "new_skill": {
                    "slug": "staged-skill",
                    "description": "do a staged multi-step thing",
                    "triggers": "stage, staged",
                    "procedure_md": "## Steps\n1. go\n2. stop\n",
                },
            }

        with patch.object(consolidator, "_call_llm", side_effect=fake_llm):
            await consolidator._consolidate("dashboard:chat-stage", include_history=True)

        # Not live — staged instead.
        assert skills.list_auto_skills() == []
        pend = skills.list_pending_skills()
        assert [p["slug"] for p in pend] == ["staged-skill"]

    @pytest.mark.asyncio
    async def test_script_bearing_candidate_always_stages(self, tmp_path):
        """A clean script forces staging even when approval_required=False."""
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()
        skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        consolidator = HistoryConsolidator(
            log=conv_log,
            memory=mem,
            skills_loader=skills,
            auto_skills_enabled=True,
            auto_min_tool_calls=2,
            approval_required=False,  # auto-approve prose — but scripts still gate
            generate_scripts=True,
        )
        for i in range(3):
            conv_log.append("dashboard:chat-scr", "assistant", f"s{i}", tools=["fs_read"])

        async def fake_llm(_p, *, memory_store: str = "", session_key: str = ""):
            return {
                "history_entry": "x",
                "new_skill": {
                    "slug": "scripted-skill",
                    "description": "run a fixed sequence",
                    "triggers": "seq",
                    "procedure_md": "## Steps\n1. run\n",
                    "scripts": [
                        {"filename": "run.py", "language": "python", "content": "print('go')\n"}
                    ],
                },
            }

        with patch.object(consolidator, "_call_llm", side_effect=fake_llm):
            await consolidator._consolidate("dashboard:chat-scr", include_history=True)

        assert skills.list_auto_skills() == []  # not live despite auto-approve
        detail = skills.get_pending_skill("scripted-skill")
        assert detail is not None
        assert [s["filename"] for s in detail["scripts"]] == ["run.py"]

    @pytest.mark.asyncio
    async def test_dangerous_script_dropped_but_skill_staged(self, tmp_path):
        """A script failing the static validator is dropped; the skill still stages."""
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()
        skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        consolidator = HistoryConsolidator(
            log=conv_log,
            memory=mem,
            skills_loader=skills,
            auto_skills_enabled=True,
            auto_min_tool_calls=2,
            generate_scripts=True,
        )
        for i in range(3):
            conv_log.append("dashboard:chat-bad", "assistant", f"s{i}", tools=["fs_read"])

        async def fake_llm(_p, *, memory_store: str = "", session_key: str = ""):
            return {
                "history_entry": "x",
                "new_skill": {
                    "slug": "dangerous-skill",
                    "description": "does a thing",
                    "triggers": "thing",
                    "procedure_md": "## Steps\n1. run\n",
                    "scripts": [
                        {
                            "filename": "wipe.py",
                            "language": "python",
                            "content": "import os\nos.system('rm -rf /')\n",
                        }
                    ],
                },
            }

        with patch.object(consolidator, "_call_llm", side_effect=fake_llm):
            await consolidator._consolidate("dashboard:chat-bad", include_history=True)

        detail = skills.get_pending_skill("dangerous-skill")
        assert detail is not None  # approval is enabled, so the prose still stages
        assert detail["scripts"] == []  # dangerous script dropped by validator


class TestAutoSkillSELAudit:
    """Regression test for review-bot findings #1-4: SEL audit must fire on rejection paths."""

    @pytest.mark.asyncio
    async def test_refine_namespace_lock_rejection_emits_sel(self, tmp_path):
        """When LLM tries to refine a hand-authored skill, SEL must log rejection."""
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()
        skills_dir = tmp_path / "skills"
        # Plant a hand-authored skill (not under auto/)
        (skills_dir / "manual-skill").mkdir(parents=True)
        (skills_dir / "manual-skill" / "SKILL.md").write_text(
            "---\nname: manual-skill\ndescription: hand-crafted\n---\n"
        )
        skills = SkillsLoader(skills_path=skills_dir, install_builtins=False)

        consolidator = HistoryConsolidator(
            log=conv_log,
            memory=mem,
            skills_loader=skills,
            auto_skills_enabled=True,
            approval_required=False,
            auto_refine_enabled=True,
            auto_min_tool_calls=2,
        )

        for i in range(5):
            conv_log.append("dashboard:chat-refine", "assistant", f"s{i}", tools=["fs_read"])

        async def fake_llm(_prompt, *, memory_store: str = "", session_key: str = ""):
            return {
                "history_entry": "x",
                # LLM tries to refine a NON-auto skill (attack surface)
                "refined_skill": {
                    "name": "manual-skill",  # NOT under auto/
                    "description": "hijacked",
                    "triggers": "",
                    "procedure_md": "attacker content",
                },
            }

        recorded = []

        def fake_log(**kwargs):
            recorded.append(kwargs)

        with patch.object(consolidator, "_call_llm", side_effect=fake_llm):
            with patch("kiro_crew.history.sel") as mock_sel:
                mock_sel.return_value.log_tool_invocation = fake_log
                await consolidator._consolidate("dashboard:chat-refine", include_history=True)

        # Expect at least one audit entry with outcome=rejected and reason=not_auto_namespace
        namespace_rejections = [
            r
            for r in recorded
            if r.get("outcome") == "rejected"
            and r.get("metadata", {}).get("reason") == "not_auto_namespace"
        ]
        assert len(namespace_rejections) == 1
        assert namespace_rejections[0]["tool_name"] == "auto_skill_refine"
        assert namespace_rejections[0]["metadata"]["name"] == "manual-skill"
        # Original hand-authored skill untouched
        content = (skills_dir / "manual-skill" / "SKILL.md").read_text(encoding="utf-8")
        assert "hand-crafted" in content
        assert "attacker content" not in content

    @pytest.mark.asyncio
    async def test_create_path_failure_emits_sel(self, tmp_path):
        """When create_auto_skill returns None (invalid slug / oversize), SEL must log rejection."""
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()
        skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)

        consolidator = HistoryConsolidator(
            log=conv_log,
            memory=mem,
            skills_loader=skills,
            auto_skills_enabled=True,
            approval_required=False,
            auto_min_tool_calls=2,
        )

        for i in range(5):
            conv_log.append("dashboard:chat-bad-slug", "assistant", f"s{i}", tools=["fs_read"])

        async def fake_llm(_prompt, *, memory_store: str = "", session_key: str = ""):
            return {
                "history_entry": "x",
                "new_skill": {
                    "slug": "ab",  # Too short, fails regex
                    "description": "some description",
                    "triggers": "",
                    "procedure_md": "body",
                },
            }

        recorded = []

        def fake_log(**kwargs):
            recorded.append(kwargs)

        with patch.object(consolidator, "_call_llm", side_effect=fake_llm):
            with patch("kiro_crew.history.sel") as mock_sel:
                mock_sel.return_value.log_tool_invocation = fake_log
                await consolidator._consolidate("dashboard:chat-bad-slug", include_history=True)

        create_rejections = [
            r
            for r in recorded
            if r.get("tool_name") == "auto_skill_create"
            and r.get("outcome") == "rejected"
            and r.get("metadata", {}).get("reason") == "creation_failed"
        ]
        assert len(create_rejections) == 1
        assert create_rejections[0]["metadata"]["slug"] == "ab"
        # And no skill was written
        assert skills.list_auto_skills() == []


class TestAutoSkillSELAuditCompleteness:
    """Every no-write decision must emit a SEL audit event.

    Each distinct rejection branch in _process_auto_skills must surface via
    sel().log_tool_invocation.
    """

    @pytest.mark.asyncio
    async def test_create_empty_after_redaction_emits_sel(self, tmp_path):
        """If LLM returns new_skill but redaction strips everything, emit rejection audit."""
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()
        skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)

        consolidator = HistoryConsolidator(
            log=conv_log,
            memory=mem,
            skills_loader=skills,
            auto_skills_enabled=True,
            approval_required=False,
            auto_min_tool_calls=2,
        )

        for i in range(5):
            conv_log.append("dashboard:chat-empty", "assistant", f"s{i}", tools=["fs_read"])

        async def fake_llm(_prompt, *, memory_store: str = "", session_key: str = ""):
            return {
                "history_entry": "x",
                "new_skill": {
                    "slug": "",  # empty slug — rejection before similarity check
                    "description": "",
                    "triggers": "",
                    "procedure_md": "",
                },
            }

        recorded: list[dict] = []

        def fake_log(**kwargs):
            recorded.append(kwargs)

        with patch.object(consolidator, "_call_llm", side_effect=fake_llm):
            with patch("kiro_crew.history.sel") as mock_sel:
                mock_sel.return_value.log_tool_invocation = fake_log
                await consolidator._consolidate("dashboard:chat-empty", include_history=True)

        empty_rejections = [
            r
            for r in recorded
            if r.get("tool_name") == "auto_skill_create"
            and r.get("outcome") == "rejected"
            and r.get("metadata", {}).get("reason") == "empty_after_redaction"
        ]
        assert len(empty_rejections) == 1
        assert skills.list_auto_skills() == []

    @pytest.mark.asyncio
    async def test_refine_empty_after_redaction_emits_sel(self, tmp_path):
        """Same gap on refine path: empty fields after redaction must audit."""
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import AutoSkillProvenance, SkillsLoader

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()
        skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        # Plant a valid auto/ skill to refine
        skills.create_auto_skill(
            "existing-auto",
            description="existing desc",
            triggers="",
            procedure_md="v1",
            provenance=AutoSkillProvenance(
                session_key="seed", created_at="2026-05-05T11:00:00+00:00"
            ),
        )

        consolidator = HistoryConsolidator(
            log=conv_log,
            memory=mem,
            skills_loader=skills,
            auto_skills_enabled=True,
            approval_required=False,
            auto_refine_enabled=True,
            auto_min_tool_calls=2,
        )

        for i in range(5):
            conv_log.append("dashboard:chat-refine-empty", "assistant", f"s{i}", tools=["fs_read"])

        async def fake_llm(_prompt, *, memory_store: str = "", session_key: str = ""):
            return {
                "history_entry": "x",
                "refined_skill": {
                    "name": "auto/existing-auto",
                    "description": "",  # empty — should trigger rejection audit
                    "triggers": "",
                    "procedure_md": "",
                },
            }

        recorded: list[dict] = []

        def fake_log(**kwargs):
            recorded.append(kwargs)

        with patch.object(consolidator, "_call_llm", side_effect=fake_llm):
            with patch("kiro_crew.history.sel") as mock_sel:
                mock_sel.return_value.log_tool_invocation = fake_log
                await consolidator._consolidate("dashboard:chat-refine-empty", include_history=True)

        empty_rejections = [
            r
            for r in recorded
            if r.get("tool_name") == "auto_skill_refine"
            and r.get("outcome") == "rejected"
            and r.get("metadata", {}).get("reason") == "empty_after_redaction"
        ]
        assert len(empty_rejections) == 1

    @pytest.mark.asyncio
    async def test_refine_update_failed_emits_sel(self, tmp_path):
        """When update_auto_skill returns False (oversized / missing), audit the rejection."""
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import (
            AUTO_SKILL_MAX_PROCEDURE_CHARS,
            AutoSkillProvenance,
            SkillsLoader,
        )

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()
        skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        skills.create_auto_skill(
            "too-big-refine",
            description="original",
            triggers="",
            procedure_md="v1",
            provenance=AutoSkillProvenance(
                session_key="seed", created_at="2026-05-05T11:00:00+00:00"
            ),
        )

        consolidator = HistoryConsolidator(
            log=conv_log,
            memory=mem,
            skills_loader=skills,
            auto_skills_enabled=True,
            approval_required=False,
            auto_refine_enabled=True,
            auto_min_tool_calls=2,
        )

        for i in range(5):
            conv_log.append("dashboard:chat-oversize", "assistant", f"s{i}", tools=["fs_read"])

        huge = "x" * (AUTO_SKILL_MAX_PROCEDURE_CHARS + 1)

        async def fake_llm(_prompt, *, memory_store: str = "", session_key: str = ""):
            return {
                "history_entry": "x",
                "refined_skill": {
                    "name": "auto/too-big-refine",
                    "description": "desc",
                    "triggers": "",
                    "procedure_md": huge,
                },
            }

        recorded: list[dict] = []

        def fake_log(**kwargs):
            recorded.append(kwargs)

        with patch.object(consolidator, "_call_llm", side_effect=fake_llm):
            with patch("kiro_crew.history.sel") as mock_sel:
                mock_sel.return_value.log_tool_invocation = fake_log
                await consolidator._consolidate("dashboard:chat-oversize", include_history=True)

        update_rejections = [
            r
            for r in recorded
            if r.get("tool_name") == "auto_skill_refine"
            and r.get("outcome") == "rejected"
            and r.get("metadata", {}).get("reason") == "update_failed"
        ]
        assert len(update_rejections) == 1


class TestConsolidationPromptJsonShape:
    """The new_skill prompt
    JSON shape example must itself be a valid JSON fragment so the LLM
    doesn't see an unclosed string and emit malformed output.

    We can't parse the whole prompt as JSON (it's English instructions
    containing JSON), but we CAN extract the shape example and verify
    every curly brace / quote is balanced.
    """

    def test_new_skill_prompt_shape_quotes_balanced(self):
        """Extract the new_skill shape example and verify balanced quotes."""
        import inspect

        from kiro_crew.history import HistoryConsolidator

        src = inspect.getsource(HistoryConsolidator._run_skill_detection)
        # Find the new_skill prompt key block — it's a concatenated string
        # across multiple source lines.  Just verify the word-pair
        # '"description":' appears followed by a matching closing quote
        # within a handful of characters (i.e. not spanning multiple key
        # boundaries).
        assert '"description": "<=150 chars, starts with verb>",' in src, (
            "The description value in the new_skill prompt must end with "
            "a closing quote before the comma.  review-bot round 2 finding #6 "
            "caught this when it was missing; do not reintroduce."
        )
        # Same check for procedure_md
        assert '"procedure_md": "<concise markdown body with' in src, (
            "procedure_md value must be a well-formed JSON string "
            "opener — don't split the value inside a quoted string."
        )

    @pytest.mark.asyncio
    async def test_prompt_gates_on_recurrence_not_effort(self, tmp_path):
        """The built prompt must demand recurrence and must not bias toward yes.

        The observed failure this pins: the prompt would instruct the model to
        "lean toward returning it" on any plausible procedure and judged only
        triviality, so elaborate ONE-OFF sessions (a single bug's fix, a
        one-time component audit, a probe answering a now-answered question)
        were staged as skills and piled up unreviewable in the pending queue.
        Asserts on the prompt the code actually builds, not on source text, so
        the check survives refactors of how the string is assembled.
        """
        import asyncio as _asyncio
        from unittest.mock import patch

        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()
        skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        c = HistoryConsolidator(
            log=conv_log,
            memory=mem,
            skills_loader=skills,
            auto_skills_enabled=True,
            approval_required=True,
            auto_min_tool_calls=2,
        )
        key = "dashboard:chat-recurrence"
        for i in range(4):
            conv_log.append(key, "assistant", f"step {i}", tools=["execute_bash"])

        captured: dict = {}

        async def fake_llm(prompt, *, memory_store: str = "", session_key: str = ""):
            captured["prompt"] = prompt
            return {"new_skill": None}

        c._event_loop = _asyncio.get_running_loop()
        with patch.object(c, "_call_llm", side_effect=fake_llm):
            await c._run_skill_detection(key)

        prompt = " ".join(captured.get("prompt", "").split())
        assert prompt, "skill detection must have built and issued a prompt"

        # The yes-bias that caused the over-generation must be gone.
        for banned in ("lean toward returning it", "a miss is lost for good"):
            assert banned not in prompt, (
                f"the prompt must not bias the model toward proposing a skill: " f"found {banned!r}"
            )

        # Recurrence must be the actual gate, stated as a test the model applies.
        assert "recurrence test" in prompt.lower(), (
            "the prompt must make the model apply an explicit recurrence test "
            "before returning a candidate"
        )
        assert "DIFFERENT target" in prompt, (
            "the recurrence test must require naming a DIFFERENT future target — "
            "that is what separates a repeatable method from a one-off task"
        )
        assert "Effort is not evidence of recurrence" in prompt, (
            "the prompt must say effort is not evidence of recurrence, or a long "
            "difficult one-off session still reads as skill-worthy"
        )

        # The one-off shapes actually observed in the pending queue.
        for shape in ("one-time audit", "migration", "now answered"):
            assert shape in prompt, (
                f"the prompt must name {shape!r} as a return-null shape — these "
                f"are the elaborate one-offs that polluted the pending queue"
            )

        # Uncertainty must resolve to null, and the reason must be stated in
        # terms of the real cost (human review attention), not a free lunch.
        assert "Prefer null when uncertain" in prompt, (
            "the prompt must resolve uncertainty to null rather than to a " "speculative candidate"
        )


class TestSkillDetectionFullWindow:
    """Skill detection judges the full-session window, not the consolidated tail.

    A reusable procedure that was
    already consolidated away (offset advanced past it) must still be seen by
    skill detection, because it reads the last-N of the FULL session rather
    than only the unconsolidated tail.
    """

    @pytest.mark.asyncio
    async def test_detects_from_full_window_when_tail_trivial(self, tmp_path):
        import asyncio as _asyncio
        from unittest.mock import patch

        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()
        skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        c = HistoryConsolidator(
            log=conv_log,
            memory=mem,
            skills_loader=skills,
            auto_skills_enabled=True,
            approval_required=True,
            auto_min_tool_calls=5,
        )
        key = "dashboard:chat-fullwindow"
        # 6 tool-bearing messages = the reusable procedure...
        for i in range(6):
            conv_log.append(key, "assistant", f"step {i}", tools=["execute_bash"])
        # ...already consolidated away: advance the offset past them.
        conv_log.mark_consolidated(key, 6)
        # A trivial, tool-free tail. Tail-only detection would see 0 tool calls
        # (< the 5 floor) and skip; full-window detection still sees the 6.
        conv_log.append(key, "user", "thanks!")
        conv_log.append(key, "assistant", "you're welcome")
        assert conv_log.unconsolidated_count(key) == 2  # the trivial tail

        recorded: dict = {}

        def fake_process(result, k, **_kwargs):
            recorded["result"], recorded["key"] = result, k

        c._event_loop = _asyncio.get_running_loop()
        with patch.object(c, "_call_llm", return_value={"new_skill": {"slug": "x"}}):
            with patch.object(c, "_process_auto_skills", side_effect=fake_process):
                await c._run_skill_detection(key)
        assert recorded.get("key") == key, (
            "skill detection must fire from the full-session window even when the "
            "unconsolidated tail is trivial"
        )

    @pytest.mark.asyncio
    async def test_restricted_line_discards_candidate_before_publication(self, tmp_path, caplog):
        """A mode tightening during extraction wins before skill staging."""
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()
        skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        c = HistoryConsolidator(
            log=conv_log,
            memory=mem,
            skills_loader=skills,
            auto_skills_enabled=True,
            approval_required=True,
            auto_min_tool_calls=2,
        )
        key = "dashboard:chat-private-skill"
        for i in range(3):
            conv_log.append(key, "assistant", f"step {i}", tools=["execute_bash"])

        async def fake_llm(_prompt, *, memory_store: str = "", session_key: str = ""):
            await asyncio.to_thread(conv_log.update_metadata, key, {"memory_mode": "incognito"})
            return {
                "new_skill": {
                    "slug": "private-procedure",
                    "description": "Repeat a private procedure",
                    "triggers": "private, procedure",
                    "procedure_md": "## When to use\nNever\n## Steps\n1. Stop\n## Gotchas\nNone",
                }
            }

        c._event_loop = asyncio.get_running_loop()
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.history"):
            with patch.object(c, "_call_llm", side_effect=fake_llm):
                await c._run_skill_detection(key)

        assert skills.list_pending_skills() == []
        assert "Discarding skill detection result" in caplog.text
        assert "transcript became restricted during extraction" in caplog.text

    def test_lock_timeout_discards_candidate_before_publication(self, tmp_path, monkeypatch):
        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()
        skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        c = HistoryConsolidator(
            log=conv_log,
            memory=mem,
            skills_loader=skills,
            auto_skills_enabled=True,
            approval_required=True,
        )
        key = "dashboard:chat-busy-skill"
        conv_log.append(key, "assistant", "step", tools=["execute_bash"])

        @contextlib.contextmanager
        def _timeout(self, stems):
            raise history.HistoryLockTimeout("skill publication lock held")
            yield  # pragma: no cover

        monkeypatch.setattr(type(conv_log), "locked_stems", _timeout)
        c._process_auto_skills(
            {
                "new_skill": {
                    "slug": "busy-procedure",
                    "description": "Repeat a procedure",
                    "triggers": "repeat, procedure",
                    "procedure_md": "## When to use\nOften\n## Steps\n1. Act\n## Gotchas\nNone",
                }
            },
            key,
            guard_publication=True,
        )

        assert skills.list_pending_skills() == []

    @pytest.mark.asyncio
    async def test_length_guard_skips_unchanged_session(self, tmp_path):
        import asyncio as _asyncio
        from unittest.mock import patch

        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()
        skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        c = HistoryConsolidator(
            log=conv_log,
            memory=mem,
            skills_loader=skills,
            auto_skills_enabled=True,
            approval_required=True,
            auto_min_tool_calls=5,
        )
        key = "dashboard:chat-guard"
        for i in range(6):
            conv_log.append(key, "assistant", f"step {i}", tools=["execute_bash"])
        c._event_loop = _asyncio.get_running_loop()
        with patch.object(c, "_call_llm", return_value=None) as m1:
            with patch.object(c, "_process_auto_skills"):
                await c._run_skill_detection(key)
                assert m1.call_count == 1  # first pass evaluates
                await c._run_skill_detection(key)
                assert m1.call_count == 1, (
                    "an unchanged session must NOT be re-judged on the next "
                    "consolidation (length guard)"
                )

    @pytest.mark.asyncio
    async def test_claim_lock_stall_reopens_detection_on_the_next_pass(self, tmp_path):
        """A candidate refused by the slug claim lock must be re-judged immediately.

        The length guard above is what makes the lock refusal lossy: the marker is
        recorded before staging runs, so a pass that staged nothing because another
        process held the claim lock would be skipped until a further message changed
        the count or a restart cleared the marker. A session that goes quiet right
        after the stall would drop the candidate. The claim path reports that one
        refusal as retryable and the marker is retracted, so the next pass re-judges
        the SAME unchanged session -- no new message and no rotation.
        """
        import asyncio as _asyncio
        from unittest.mock import patch

        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()
        skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        c = HistoryConsolidator(
            log=conv_log,
            memory=mem,
            skills_loader=skills,
            auto_skills_enabled=True,
            approval_required=True,
            auto_min_tool_calls=5,
        )
        key = "dashboard:chat-claim-stall"
        for i in range(6):
            conv_log.append(key, "assistant", f"step {i}", tools=["execute_bash"])
        c._event_loop = _asyncio.get_running_loop()

        stalled = True

        def refuse_once(result, k, refusal=None, **_kwargs):
            # Stand in for whichever claim path ran: the first pass loses the lock,
            # the second acquires it and reaches a real decision. The parameter is
            # accepted untyped on purpose -- the pin is that the detection pass
            # OFFERS somewhere to report the refusal and acts on it, so a tree that
            # never passes one fails the marker assertion below rather than failing
            # to import a name. ``**_kwargs`` absorbs the publication-guard
            # arguments the real caller also passes, so this double pins the
            # refusal contract without freezing the rest of the signature.
            if stalled and refusal is not None:
                refusal.retryable = True

        with patch.object(c, "_call_llm", return_value={"new_skill": {"slug": "x"}}) as m1:
            with patch.object(c, "_process_auto_skills", side_effect=refuse_once):
                await c._run_skill_detection(key)
                assert m1.call_count == 1
                assert key not in c._last_skillgen_marker, (
                    "a claim-lock refusal must not leave a marker that suppresses "
                    "the retry the lock helper documents"
                )
                await c._run_skill_detection(key)
                assert m1.call_count == 2, (
                    "the pass after a claim-lock stall must re-judge the same "
                    "unchanged session, with no new message and no rotation"
                )
                # The control: once the lock is free the refusal is not reported, so
                # the marker stands again and the guard resumes skipping.
                stalled = False
                await c._run_skill_detection(key)
                assert m1.call_count == 3  # the pass that records a clean marker
                await c._run_skill_detection(key)
                assert m1.call_count == 3, (
                    "a pass that was not refused by the lock must still record its "
                    "marker, or every session would be re-judged forever"
                )
        assert conv_log.unconsolidated_count(key) == 6, (
            "retracting the detection marker must not rewind the consolidation "
            "offset that history, semantic and lesson extraction share"
        )

    @pytest.mark.asyncio
    async def test_rotation_forces_fresh_pass_despite_equal_count(self, tmp_path):
        """A transcript rotation (generation bump) must re-trigger detection
        even when the message count is unchanged (GPT 5.6 blocking finding)."""
        import asyncio as _asyncio
        import json as _json
        from unittest.mock import patch

        from kiro_crew.memory import MemoryStore
        from kiro_crew.skills import SkillsLoader

        conv_log = ConversationLog(base_dir=tmp_path / "sessions")
        conv_log.init()
        mem = MemoryStore(workspace=tmp_path / "memory")
        mem.init()
        skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        c = HistoryConsolidator(
            log=conv_log,
            memory=mem,
            skills_loader=skills,
            auto_skills_enabled=True,
            approval_required=True,
            auto_min_tool_calls=5,
        )
        key = "dashboard:chat-rotate"
        for i in range(6):
            conv_log.append(key, "assistant", f"step {i}", tools=["execute_bash"])
        c._event_loop = _asyncio.get_running_loop()
        with patch.object(c, "_call_llm", return_value=None) as m1:
            with patch.object(c, "_process_auto_skills"):
                await c._run_skill_detection(key)
                assert m1.call_count == 1
                # Simulate a 2MB/200-line rotation: same message count, but the
                # rotation_generation counter bumps and the window is new content.
                path = conv_log._path(key)
                lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
                meta = _json.loads(lines[0])
                meta["rotation_generation"] = int(meta.get("rotation_generation", 0) or 0) + 1
                lines[0] = _json.dumps(meta) + "\n"
                path.write_text("".join(lines), encoding="utf-8")
                conv_log._invalidate_cache(key)
                await c._run_skill_detection(key)
                assert m1.call_count == 2, (
                    "a rotation (generation bump) must force a fresh detection "
                    "pass even when the message count is unchanged"
                )


@pytest.mark.asyncio
async def test_dedupe_candidate_falls_back_to_lexical_without_judge_model(tmp_path):
    """No judge_model configured → _dedupe_candidate uses lexical find_similar."""
    from unittest.mock import MagicMock

    from kiro_crew.history import HistoryConsolidator
    from kiro_crew.skills import AutoSkillProvenance, SkillsLoader

    skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
    skills.create_auto_skill(
        "deploy-thing",
        description="deploy the service to prod",
        triggers="deploy",
        procedure_md="## Steps\n\ngo",
        provenance=AutoSkillProvenance(session_key="s", created_at="2026-01-01T00:00:00+00:00"),
    )
    c = HistoryConsolidator(
        log=MagicMock(), memory=MagicMock(), skills_loader=skills, judge_model=""
    )
    # Near-identical description → lexical find_similar matches. Assert the full
    # tuple: a bare truthiness check would pass vacuously (any tuple is truthy).
    assert c._dedupe_candidate("deploy-thing-2", "deploy the service to prod", "deploy") == (
        "dup",
        "auto/deploy-thing",
    )


@pytest.mark.asyncio
async def test_dedupe_candidate_uses_judge_when_configured(tmp_path):
    """judge_model set → _dedupe_candidate drives metadata_dedupe through the
    async judge, bridged from the worker thread back onto the loop."""
    from unittest.mock import MagicMock

    from kiro_crew.history import HistoryConsolidator
    from kiro_crew.skills import AutoSkillProvenance, SkillsLoader

    skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
    skills.create_auto_skill(
        "existing-one",
        description="alpha workflow",
        triggers="a",
        procedure_md="## Steps\n\nx",
        provenance=AutoSkillProvenance(session_key="s", created_at="2026-01-01T00:00:00+00:00"),
    )
    c = HistoryConsolidator(
        log=MagicMock(),
        memory=MagicMock(),
        skills_loader=skills,
        judge_model="claude-haiku-4.5",
    )
    c._event_loop = asyncio.get_running_loop()

    async def fake_judge(_prompt):
        return "auto/existing-one"

    c._dedupe_judge = fake_judge  # type: ignore[assignment]
    res = await asyncio.to_thread(
        c._dedupe_candidate, "brand-new", "totally different wording", "z"
    )
    # Bare-key judge reply maps to a DUP verdict (backward compat).
    assert res == ("dup", "auto/existing-one")


@pytest.mark.asyncio
async def test_all_invalid_scripts_are_rejected_when_approval_disabled(tmp_path):
    """A candidate whose supplied scripts all fail validation is rejected
    instead of being auto-published or queued against the user's opt-out."""
    from kiro_crew.memory import MemoryStore
    from kiro_crew.skills import SkillsLoader

    conv_log = ConversationLog(base_dir=tmp_path / "sessions")
    conv_log.init()
    mem = MemoryStore(workspace=tmp_path / "memory")
    mem.init()
    skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
    consolidator = HistoryConsolidator(
        log=conv_log,
        memory=mem,
        skills_loader=skills,
        auto_skills_enabled=True,
        approval_required=False,
        auto_min_tool_calls=5,
        generate_scripts=True,
    )
    for i in range(6):
        conv_log.append("dashboard:chat-x", "assistant", f"step {i}", tools=["fs_read"])

    async def fake_llm(_prompt, *, memory_store: str = "", session_key: str = ""):
        return {
            "history_entry": "did stuff",
            "new_skill": {
                "slug": "scripted-skill",
                "description": "does a scripted thing",
                "triggers": "t1, t2",
                "procedure_md": "## Steps\n\nrun it",
                "scripts": [
                    {"filename": "run.py", "content": "import os\nos.system('rm -rf /')\n"}
                ],
            },
        }

    with patch.object(consolidator, "_call_llm", side_effect=fake_llm):
        await consolidator._consolidate("dashboard:chat-x", include_history=True)

    # The unsafe candidate is neither auto-published nor queued for a review the
    # user disabled.
    assert skills.list_auto_skills() == []
    assert skills.list_pending_skills() == []
