"""The shipped system prompts must keep the full-PR-URL output rule.

The dashboard's Changes panel (PR state, checks, review threads) is built by
scanning the agent's own message text for full ``/pull/<n>`` or
``/-/merge_requests/<n>`` links; tool output is not part of the persisted
transcript, so nothing else can supply the link. A prompt rewrite that drops
this rule silently removes the panel, which is why it is pinned by a test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

CONFIG_DIR = Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "config"
PROMPTS = ("prompt.md", "prompt-orchestrator.md")


@pytest.mark.parametrize("name", PROMPTS)
def test_prompt_requires_full_pull_request_url(name: str) -> None:
    text = (CONFIG_DIR / name).read_text(encoding="utf-8")
    assert "full URL" in text
    # Both provider shapes the frontend parser recognizes are shown as examples.
    assert "/pull/" in text
    assert "/-/merge_requests/" in text
