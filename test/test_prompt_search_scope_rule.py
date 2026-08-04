"""The shipped system prompts must keep the file-search-scope rule.

KiroCrew's only lever over the kiro-cli-native ``grep``/``glob`` tools is the
system prompt -- those tools are not mediated by the MCP gateway, so nothing in
Python can cap their search root. Without an explicit rule the agent recursively
searches the entire home directory (``$HOME``) whenever the active project is
broad (``slot.project`` may legitimately be ``$HOME``) or the target is not in
the immediate workspace. On a real home tree (``~/Repos``, caches,
``node_modules``) that walk takes a very long time and is almost never the right
scope -- the exact behaviour users complain about. The rule is the fix, so it is
pinned by a test to survive prompt rewrites.
"""

from __future__ import annotations

from pathlib import Path

import pytest

CONFIG_DIR = Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "config"
PROMPTS = ("prompt.md", "prompt-orchestrator.md")


@pytest.mark.parametrize("name", PROMPTS)
def test_prompt_requires_search_scope_rule(name: str) -> None:
    text = (CONFIG_DIR / name).read_text(encoding="utf-8")
    # Distinctive phrase from the rule; keep in sync with config/prompt*.md.
    assert "never walk the whole home directory" in text
    # The rule must steer toward a bounded scope, not just forbid the broad one.
    assert "$HOME" in text
