"""Tests for the prompt-text blocks ``ContextBuilder`` adds outside memory.

This module pins multi-byte punctuation sanitization, the ``[CURRENT DATE]``
line's configured time zone, steering-resource loading and its per-backend
injection, and the keep-visible and gender clauses of the critical rules. The
core turn shape lives in ``test_context.py`` and the memory and lesson blocks in
``test_context_memory_lessons.py``.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kiro_crew.context import ContextBuilder
from kiro_crew.hooks import HookManager, HooksConfig
from kiro_crew.learn import LessonStore
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader

# Each context-builder file keeps this xdist group; ``test_context_memory_lessons``'s ratchet
# walks the package through the shared ``source_corpus`` (one memoised file list per
# process, texts streamed and parsed only for the files that can match), so its cost is
# now a second rather than the ~30s full-tree ``ast.parse`` this group was first added
# for; the group stays so the file-list cache is paid once per run, not once per worker.
pytestmark = pytest.mark.xdist_group(name="tree_scan_test_context")


@pytest.fixture(autouse=True)
def _close_skills_loaders(close_skills_loaders):
    """Every test here builds a ``ContextBuilder``: close its ``SkillsLoader`` (``test/conftest.py``)."""


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
        # The PUBLISHED default, not a config load: get_local_tz reads the
        # snapshot so prompt assembly does no config I/O on the event loop.
        with patch("kiro_crew.cron.published_config_timezone", return_value="Asia/Tokyo"):
            ctx = builder.build_session_context()
        # Tokyo is JST/UTC+9; %Z renders "JST"
        assert "[CURRENT DATE]" in ctx
        date_line = [ln for ln in ctx.splitlines() if ln.startswith("[CURRENT DATE]")][0]
        assert "JST" in date_line

    def test_current_date_falls_back_to_utc_when_config_empty(self, tmp_path):
        builder = self._make_builder(tmp_path)
        with patch("kiro_crew.cron.published_config_timezone", return_value=""):
            ctx = builder.build_session_context()
        date_line = [ln for ln in ctx.splitlines() if ln.startswith("[CURRENT DATE]")][0]
        assert "UTC" in date_line


class TestLoadSteeringResources:
    """Tests for _load_steering_resources."""

    def test_loads_md_files_from_resources(self, tmp_path):
        from kiro_crew.context import _load_steering_resources

        steering_dir = tmp_path / ".kiro" / "steering"
        steering_dir.mkdir(parents=True)
        (steering_dir / "rules.md").write_text("# My Rules\nAlways be nice.")

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
        ``--agent`` (acp/client_start.py ``_spawn``), so build_session_context must
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


class TestKeepVisibleMarkerRule:
    """The keep-visible collapse exemption must be documented in the DASHBOARD
    critical rules (collapse-all is a dashboard-transcript feature and rehype-raw
    is what renders the marker invisible), and must NOT ship in the channel
    variant -- Slack/Discord outbound formatters never strip HTML comments, so a
    channel agent following the rule would show users the literal marker text."""

    def test_marker_documented_in_dashboard_rules_only(self):
        from kiro_crew.context import _CRITICAL_RULES, _CRITICAL_RULES_CHANNEL

        assert "<!-- keep-visible -->" in _CRITICAL_RULES
        assert "<!-- keep-visible -->" not in _CRITICAL_RULES_CHANNEL

    def test_prefers_restructuring_over_marker(self):
        from kiro_crew.context import _CRITICAL_RULES, _CRITICAL_RULES_CHANNEL

        clause = "Prefer restructuring the turn so the deliverable IS its last message"
        assert clause in _CRITICAL_RULES
        assert clause not in _CRITICAL_RULES_CHANNEL


def test_critical_rules_forbid_assuming_a_persons_gender():
    """A named person whose pronouns were never given is not called "he"."""
    from kiro_crew.context import _CRITICAL_RULES, _CRITICAL_RULES_CHANNEL

    rule = "Do not assume anyone's gender."
    assert rule in _CRITICAL_RULES
    assert rule in _CRITICAL_RULES_CHANNEL
