"""Tests for `{{WIDGET_BLOCK}}` placeholder resolution in context.py.

Before the widgets skill was introduced, `_resolve_prompt_templates` injected
~200 words of theme-variable rules + a `[WIDGETS]` per-message append that
duplicated the same content. Both have been replaced with a short pointer at
the system-prompt level and a bundled `widgets` skill the agent cats on
demand. These tests lock the new shape so we don't accidentally reintroduce
the bloat.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch


def _resolve(prompt: str, session_key: str, density: str = "more") -> str:
    """Call the private template resolver with a canned config."""
    from kiro_crew.context import ContextBuilder

    fake_cfg = SimpleNamespace(dashboard=SimpleNamespace(widget_density=density))
    with patch("kiro_crew.context.KiroCrewConfig.load", return_value=fake_cfg):
        return ContextBuilder._resolve_prompt_templates(prompt, session_key)


class TestWidgetBlockPlaceholder:
    """`{{WIDGET_BLOCK}}` expands to a short skill pointer on dashboard; empty elsewhere."""

    def test_non_dashboard_strips_placeholder(self):
        # Slack / CLI / channel sessions cannot render widgets; the placeholder
        # MUST expand to empty so the prompt stays lean.
        prompt = "prefix {{WIDGET_BLOCK}} suffix"
        for key in ("slack:C123:123.456", "cli:local", "channel:Cabc", ""):
            result = _resolve(prompt, key)
            assert "{{WIDGET_BLOCK}}" not in result
            assert "mcwidget" not in result.lower()
            assert result == "prefix  suffix"

    def test_dashboard_more_density_emits_pointer(self):
        # The `more` branch should encourage widgets and point at the skill.
        result = _resolve("{{WIDGET_BLOCK}}", "dashboard:abc", density="more")
        assert "## Inline Widgets" in result
        assert "<mcwidget" in result
        assert "`widgets` skill" in result

    def test_dashboard_less_density_emits_pointer(self):
        # The `less` branch should discourage widgets but still point at the skill.
        result = _resolve("{{WIDGET_BLOCK}}", "dashboard:abc", density="less")
        assert "## Inline Widgets" in result
        assert "<mcwidget" in result
        assert "`widgets` skill" in result
        assert "prefer" in result.lower()

    def test_pointer_does_not_restate_skill_content(self):
        # The main-prompt pointer must NOT inline the theme-variable table,
        # the full rules, or per-Tailwind-class guidance. That lives in the
        # bundled skill. Regression guard against reintroducing the bloat.
        for density in ("more", "less"):
            result = _resolve("{{WIDGET_BLOCK}}", "dashboard:abc", density=density)
            assert "var(--bg)" not in result, f"theme var leaked into {density} pointer"
            assert "var(--card)" not in result
            assert "Chart.js" not in result
            assert "bg-[var(" not in result

    def test_pointer_is_short(self):
        # Hard budget: each pointer branch stays under 300 chars. The old
        # block was ~800. Catches accidental regrowth.
        for density in ("more", "less"):
            result = _resolve("{{WIDGET_BLOCK}}", "dashboard:abc", density=density)
            assert len(result) < 300, f"{density} pointer too long: {len(result)} chars"

    def test_dashboard_underscore_key_also_matches(self):
        # Some dashboard sessions use `dashboard_<slot>` instead of `dashboard:<slot>`.
        result = _resolve("{{WIDGET_BLOCK}}", "dashboard_slot1", density="more")
        assert "<mcwidget" in result

    def test_density_default_when_config_missing(self):
        # If the dashboard config omits widget_density entirely, fall back to "more".
        from kiro_crew.context import ContextBuilder

        fake_cfg = SimpleNamespace(dashboard=SimpleNamespace())
        with patch("kiro_crew.context.KiroCrewConfig.load", return_value=fake_cfg):
            result = ContextBuilder._resolve_prompt_templates("{{WIDGET_BLOCK}}", "dashboard:x")
        # The `more` branch fires (skill pointer + encouraging wording) and the
        # `less` branch does not. Assert structurally, not on specific prose
        # tokens — the wording may evolve.
        assert "`widgets` skill" in result, "skill pointer missing"
        assert "prefer" not in result.lower(), "less-branch wording leaked"
