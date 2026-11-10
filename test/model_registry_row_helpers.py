"""A model registry row with no kiro-cli id, for tests of the refusal it triggers.

A shipped row for a model kiro-cli serves carries that model's ``acp`` id, so no
shipped Claude row is one ``model_registry.lacks_acp_id`` flags. A test of what
Kiro Crew does with such a row adds one: :func:`add_claude_only_row` copies the
registry, adds a row whose only provider is ``claude_code`` and rebuilds the
lookup indices into fresh dicts, so ``monkeypatch`` restores the shipped registry
and its indices when the test ends.
"""

from __future__ import annotations

import pytest

from kiro_crew import model_registry

#: The added row's canonical key.
CLAUDE_ONLY_KEY = "test-only-1-1m"
#: A registered spelling of the added row that kiro-cli has no id for.
CLAUDE_ONLY_ALIAS = "claude-test-only-1"
#: The added row's ``claude_code`` id.
CLAUDE_ONLY_ID = "global.anthropic.claude-test-only-1[1m]"


def add_claude_only_row(monkeypatch: pytest.MonkeyPatch) -> str:
    """Register a row with no ``acp`` id for the test's duration and return its alias."""
    registry = dict(model_registry._REGISTRY)
    registry[CLAUDE_ONLY_KEY] = {
        "display": "Test Only 1 (1M context)",
        "window": 1_000_000,
        "aliases": [CLAUDE_ONLY_ALIAS, "claude-test-only-1[1m]"],
        "providers": {"claude_code": CLAUDE_ONLY_ID},
    }
    monkeypatch.setattr(model_registry, "_REGISTRY", registry)
    monkeypatch.setattr(model_registry, "_CANONICAL_INDEX", {})
    monkeypatch.setattr(model_registry, "_CANONICAL_INDEX_FOLDED", {})
    monkeypatch.setattr(model_registry, "_DEFAULTS", {})
    model_registry._build_indices()
    return CLAUDE_ONLY_ALIAS
