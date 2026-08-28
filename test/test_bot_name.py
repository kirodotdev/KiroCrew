"""Tests for configurable bot_name — substitution, defaults, sanitization."""

from __future__ import annotations

from kiro_crew.config.loader import _sanitize_bot_name


class TestSanitizeBotName:
    def test_normal_name(self):
        assert _sanitize_bot_name("Alita") == "Alita"

    def test_empty_returns_empty(self):
        assert _sanitize_bot_name("") == ""

    def test_strips_braces(self):
        assert _sanitize_bot_name("{bot_name}") == "bot_name"

    def test_strips_markdown(self):
        assert _sanitize_bot_name("**Bold**") == "Bold"

    def test_max_length(self):
        assert len(_sanitize_bot_name("A" * 100)) == 50

    def test_non_string(self):
        assert _sanitize_bot_name(123) == ""  # type: ignore[arg-type]

    def test_whitespace_stripped(self):
        assert _sanitize_bot_name("  Kiro  ") == "Kiro"


class TestBotNameSubstitution:
    def test_custom_name_substituted(self):
        from kiro_crew.context import ContextBuilder

        ctx = ContextBuilder(bot_name="Alita")
        assert ctx._substitute_bot_name("You are {bot_name} 🐾") == "You are Alita 🐾"

    def test_empty_defaults_from_config(self):
        from unittest.mock import patch

        from kiro_crew.acp.types import ACP_BACKEND_CLAUDE, ACP_BACKEND_KIRO
        from kiro_crew.context import ContextBuilder

        # Keyed on agent.acp_backend, NOT agent.provider. The provider enum is
        # single-valued ("acp"), so a `provider == "claude_code"` comparison can
        # never be true at runtime: mocking that state asserted a branch
        # production could not reach. The branded persona name belongs to the
        # claude BACKEND, which is a state an operator can actually select.
        with patch("kiro_crew.context.KiroCrewConfig.load") as mock_cfg:
            mock_cfg.return_value.agent.acp_backend = ACP_BACKEND_KIRO
            ctx = ContextBuilder(bot_name="")
            assert ctx._substitute_bot_name("You are {bot_name}.") == "You are Kiro."

        with patch("kiro_crew.context.KiroCrewConfig.load") as mock_cfg:
            mock_cfg.return_value.agent.acp_backend = ACP_BACKEND_CLAUDE
            ctx = ContextBuilder(bot_name="")
            # brand-ok: the persona name main already ships, not a brand typo.
            assert ctx._substitute_bot_name("You are {bot_name}.") == "You are KiroCrew."

        # An unrelated backend must NOT inherit claude's branded persona.
        with patch("kiro_crew.context.KiroCrewConfig.load") as mock_cfg:
            mock_cfg.return_value.agent.acp_backend = "codex"
            ctx = ContextBuilder(bot_name="")
            assert ctx._substitute_bot_name("You are {bot_name}.") == "You are Kiro."

    def test_no_placeholder_is_noop(self):
        from kiro_crew.context import ContextBuilder

        ctx = ContextBuilder(bot_name="Alita")
        assert ctx._substitute_bot_name("No placeholder here.") == "No placeholder here."

    def test_runtime_label_refreshes_default_name_after_backend_switch(self):
        from unittest.mock import patch

        from kiro_crew.acp.types import ACP_BACKEND_KIRO, PROVIDER_LABEL_CLAUDE
        from kiro_crew.context import ContextBuilder

        with patch("kiro_crew.context.KiroCrewConfig.load") as mock_cfg:
            mock_cfg.return_value.agent.acp_backend = ACP_BACKEND_KIRO
            ctx = ContextBuilder(bot_name="")

        assert ctx._substitute_bot_name("You are {bot_name}.", PROVIDER_LABEL_CLAUDE) == (
            "You are KiroCrew."  # brand-ok
        )
        assert ctx._substitute_bot_name("You are {bot_name}.", "codex") == "You are Kiro."

    def test_self_referential_no_recursion(self):
        """bot_name containing {bot_name} — braces stripped by sanitizer."""
        from kiro_crew.context import ContextBuilder

        name = _sanitize_bot_name("{bot_name}")
        ctx = ContextBuilder(bot_name=name)
        assert ctx._substitute_bot_name("You are {bot_name}.") == "You are bot_name."
