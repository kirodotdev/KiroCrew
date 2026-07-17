"""Tests for CC agent name resolution from cc-plugins directory.

Tests _resolve_cc_agent_name and _list_all_agent_names functions.
"""

from __future__ import annotations

from pathlib import Path

from kiro_crew.slack.handler import _list_all_agent_names, _resolve_cc_agent_name

# ── Helpers ────────────────────────────────────────────────────────────────


def _write_agent_md(base_dir: Path, plugin_name: str, agent_name: str, extra: str = "") -> Path:
    """Create a minimal agent .md file with frontmatter containing the given name."""
    agents_dir = base_dir / plugin_name / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    md_path = agents_dir / f"{agent_name}.md"
    md_path.write_text(
        f"---\nname: {agent_name}\n{extra}---\n\n# Agent {agent_name}\n",
        encoding="utf-8",
    )
    return md_path


def _write_agent_md_quoted(base_dir: Path, plugin_name: str, agent_name: str) -> Path:
    """Create an agent .md file with quoted name in frontmatter."""
    agents_dir = base_dir / plugin_name / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    md_path = agents_dir / f"{agent_name}.md"
    md_path.write_text(
        f'---\nname: "{agent_name}"\n---\n\n# Agent {agent_name}\n',
        encoding="utf-8",
    )
    return md_path


# ── _resolve_cc_agent_name tests ──────────────────────────────────────────


class TestResolveCcAgentName:
    """Tests for _resolve_cc_agent_name."""

    def test_exact_match(self, tmp_path: Path) -> None:
        """Exact name match returns the agent name."""
        _write_agent_md(tmp_path, "my-plugin", "code-reviewer")
        result = _resolve_cc_agent_name("code-reviewer", cc_plugins_dir=tmp_path)
        assert result == "code-reviewer"

    def test_exact_match_quoted_name(self, tmp_path: Path) -> None:
        """Quoted name in frontmatter is resolved correctly."""
        _write_agent_md_quoted(tmp_path, "my-plugin", "assistant")
        result = _resolve_cc_agent_name("assistant", cc_plugins_dir=tmp_path)
        assert result == "assistant"

    def test_case_sensitivity(self, tmp_path: Path) -> None:
        """Name matching is case-sensitive."""
        _write_agent_md(tmp_path, "my-plugin", "CodeReviewer")
        # Exact case should match
        assert _resolve_cc_agent_name("CodeReviewer", cc_plugins_dir=tmp_path) == "CodeReviewer"
        # Different case should NOT match
        assert _resolve_cc_agent_name("codereviewer", cc_plugins_dir=tmp_path) is None
        assert _resolve_cc_agent_name("CODEREVIEWER", cc_plugins_dir=tmp_path) is None

    def test_missing_dir(self, tmp_path: Path) -> None:
        """Non-existent cc-plugins dir returns None."""
        nonexistent = tmp_path / "does-not-exist"
        result = _resolve_cc_agent_name("anything", cc_plugins_dir=nonexistent)
        assert result is None

    def test_no_match(self, tmp_path: Path) -> None:
        """Name not matching any agent returns None."""
        _write_agent_md(tmp_path, "my-plugin", "alpha")
        _write_agent_md(tmp_path, "my-plugin", "beta")
        result = _resolve_cc_agent_name("gamma", cc_plugins_dir=tmp_path)
        assert result is None

    def test_multiple_agents_first_match(self, tmp_path: Path) -> None:
        """When multiple agents exist, the correct one is matched."""
        _write_agent_md(tmp_path, "plugin-a", "agent-one")
        _write_agent_md(tmp_path, "plugin-b", "agent-two")
        _write_agent_md(tmp_path, "plugin-c", "agent-three")

        assert _resolve_cc_agent_name("agent-one", cc_plugins_dir=tmp_path) == "agent-one"
        assert _resolve_cc_agent_name("agent-two", cc_plugins_dir=tmp_path) == "agent-two"
        assert _resolve_cc_agent_name("agent-three", cc_plugins_dir=tmp_path) == "agent-three"

    def test_file_without_frontmatter_skipped(self, tmp_path: Path) -> None:
        """Files without YAML frontmatter are skipped."""
        agents_dir = tmp_path / "plugin" / "agents"
        agents_dir.mkdir(parents=True)
        md_path = agents_dir / "no-frontmatter.md"
        md_path.write_text("# Just a regular markdown file\nNo frontmatter here.\n", encoding="utf-8")

        result = _resolve_cc_agent_name("no-frontmatter", cc_plugins_dir=tmp_path)
        assert result is None

    def test_malformed_frontmatter_skipped(self, tmp_path: Path) -> None:
        """Files with malformed frontmatter are skipped gracefully."""
        agents_dir = tmp_path / "plugin" / "agents"
        agents_dir.mkdir(parents=True)
        md_path = agents_dir / "broken.md"
        # Has opening --- but no closing ---
        md_path.write_text("---\nname: broken\nno closing delimiter", encoding="utf-8")

        result = _resolve_cc_agent_name("broken", cc_plugins_dir=tmp_path)
        assert result is None

    def test_empty_name_field(self, tmp_path: Path) -> None:
        """Agent with empty name field does not match empty string lookup."""
        agents_dir = tmp_path / "plugin" / "agents"
        agents_dir.mkdir(parents=True)
        md_path = agents_dir / "empty.md"
        md_path.write_text("---\nname: \n---\n\n# Empty\n", encoding="utf-8")

        result = _resolve_cc_agent_name("", cc_plugins_dir=tmp_path)
        assert result is None

    def test_symlink_to_sensitive_path_rejected(self, tmp_path: Path, monkeypatch) -> None:
        """A symlinked agent .md whose target is a sensitive path is not read.

        Guards against symlink traversal into credential files: the read must go
        through hooks.safe_read_file_bytes(), which enforces is_sensitive_path().
        """
        agents_dir = tmp_path / "plugin" / "agents"
        agents_dir.mkdir(parents=True)
        # A real secret file with valid frontmatter that, if read, would "match".
        secret = tmp_path / "credentials"
        secret.write_text("---\nname: leaked\n---\n", encoding="utf-8")
        link = agents_dir / "leaked.md"
        link.symlink_to(secret)

        # Force the hooks layer to treat the secret target as sensitive.
        import kiro_crew.hooks as hooks

        real_is_sensitive = hooks.is_sensitive_path

        def fake_is_sensitive(path: str) -> bool:
            if "credentials" in str(path):
                return True
            return real_is_sensitive(path)

        monkeypatch.setattr(hooks, "is_sensitive_path", fake_is_sensitive)

        # The sensitive target is rejected, so no name resolves.
        assert _resolve_cc_agent_name("leaked", cc_plugins_dir=tmp_path) is None


# ── _list_all_agent_names tests ───────────────────────────────────────────


class TestListAllAgentNames:
    """Tests for _list_all_agent_names."""

    def test_empty_dir(self, tmp_path: Path) -> None:
        """Empty cc-plugins directory returns '(none found)' when no kiro agents exist either."""
        # Create empty cc-plugins dir
        cc_dir = tmp_path / "cc-plugins"
        cc_dir.mkdir()
        # Point kiro dir somewhere empty too
        kiro_dir = tmp_path / "kiro-agents"
        kiro_dir.mkdir()

        # Monkey-patch Path.home temporarily
        import unittest.mock

        with unittest.mock.patch("kiro_crew.slack.handler.Path.home", return_value=tmp_path):
            # kiro agents dir needs to be at tmp_path / ".kiro" / "agents"
            kiro_agents = tmp_path / ".kiro" / "agents"
            kiro_agents.mkdir(parents=True)
            result = _list_all_agent_names(cc_plugins_dir=cc_dir)
        assert result == "(none found)"

    def test_multiple_agents(self, tmp_path: Path) -> None:
        """Multiple agents from cc-plugins are listed."""
        cc_dir = tmp_path / "cc-plugins"
        _write_agent_md(cc_dir, "plugin-a", "alpha-agent")
        _write_agent_md(cc_dir, "plugin-b", "beta-agent")

        import unittest.mock

        with unittest.mock.patch("kiro_crew.slack.handler.Path.home", return_value=tmp_path):
            kiro_agents = tmp_path / ".kiro" / "agents"
            kiro_agents.mkdir(parents=True)
            result = _list_all_agent_names(cc_plugins_dir=cc_dir)

        assert "alpha-agent" in result
        assert "beta-agent" in result

    def test_missing_dir(self, tmp_path: Path) -> None:
        """Non-existent cc-plugins dir still works (returns kiro agents or none)."""
        nonexistent = tmp_path / "does-not-exist"
        import unittest.mock

        with unittest.mock.patch("kiro_crew.slack.handler.Path.home", return_value=tmp_path):
            kiro_agents = tmp_path / ".kiro" / "agents"
            kiro_agents.mkdir(parents=True)
            result = _list_all_agent_names(cc_plugins_dir=nonexistent)

        assert result == "(none found)"

    def test_sort_order_deterministic(self, tmp_path: Path) -> None:
        """Agent listing is deterministic (sorted by file path)."""
        cc_dir = tmp_path / "cc-plugins"
        # Create agents in non-alphabetical plugin order
        _write_agent_md(cc_dir, "z-plugin", "zulu")
        _write_agent_md(cc_dir, "a-plugin", "alpha")
        _write_agent_md(cc_dir, "m-plugin", "mike")

        import unittest.mock

        with unittest.mock.patch("kiro_crew.slack.handler.Path.home", return_value=tmp_path):
            kiro_agents = tmp_path / ".kiro" / "agents"
            kiro_agents.mkdir(parents=True)
            result = _list_all_agent_names(cc_plugins_dir=cc_dir)

        names = [n.strip() for n in result.split(",")]
        # Since we glob with sorted(), a-plugin comes first
        assert names[0] == "alpha"
        assert "mike" in names
        assert "zulu" in names

    def test_kirocrew_lite_excluded(self, tmp_path: Path) -> None:
        """The kirocrew-lite agent is excluded from listings."""
        cc_dir = tmp_path / "cc-plugins"
        _write_agent_md(cc_dir, "core", "kirocrew-lite")
        _write_agent_md(cc_dir, "core", "useful-agent")

        import unittest.mock

        with unittest.mock.patch("kiro_crew.slack.handler.Path.home", return_value=tmp_path):
            kiro_agents = tmp_path / ".kiro" / "agents"
            kiro_agents.mkdir(parents=True)
            result = _list_all_agent_names(cc_plugins_dir=cc_dir)

        assert "kirocrew-lite" not in result
        assert "useful-agent" in result

    def test_kirocrew_lite_excluded_from_kiro_agents(self, tmp_path: Path) -> None:
        """A ~/.kiro/agents/kirocrew-lite.json must ALSO be hidden.

        Regression guard for the AutoSDE finding: the kirocrew-lite filter was
        only applied to cc-plugins agents, so a kiro-side kirocrew-lite.json
        leaked into the listing despite the docstring claiming it is hidden.
        """
        cc_dir = tmp_path / "cc-plugins"
        _write_agent_md(cc_dir, "core", "useful-agent")

        import unittest.mock

        with unittest.mock.patch("kiro_crew.slack.handler.Path.home", return_value=tmp_path):
            kiro_agents = tmp_path / ".kiro" / "agents"
            kiro_agents.mkdir(parents=True)
            (kiro_agents / "kirocrew-lite.json").write_text(
                '{"name": "kirocrew-lite"}', encoding="utf-8"
            )
            (kiro_agents / "real-kiro.json").write_text(
                '{"name": "real-kiro"}', encoding="utf-8"
            )
            result = _list_all_agent_names(cc_plugins_dir=cc_dir)

        assert "kirocrew-lite" not in result
        assert "real-kiro" in result
        assert "useful-agent" in result

    def test_deduplication_with_kiro_agents(self, tmp_path: Path) -> None:
        """Agents already present from kiro are not duplicated in cc-plugins listing."""
        cc_dir = tmp_path / "cc-plugins"
        _write_agent_md(cc_dir, "plugin", "shared-agent")

        import unittest.mock

        with unittest.mock.patch("kiro_crew.slack.handler.Path.home", return_value=tmp_path):
            kiro_agents = tmp_path / ".kiro" / "agents"
            kiro_agents.mkdir(parents=True)
            # Create a kiro agent JSON with same stem
            (kiro_agents / "shared-agent.json").write_text('{"name": "shared-agent"}', encoding="utf-8")
            result = _list_all_agent_names(cc_plugins_dir=cc_dir)

        # Should appear only once
        assert result.count("shared-agent") == 1
