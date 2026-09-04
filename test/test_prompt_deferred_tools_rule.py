"""The shipped system prompts must not promise directly callable MCP tools.

``agent.tool_search`` ships enabled, and Tool Search's only precondition is one
configured MCP server -- which a Kiro Crew install always has, because it
registers its own managed ``kirocrew-core`` / ``kirocrew-cron``. Every MCP tool
is therefore deferred: its spec is absent from the model's tool list until
``tool_search`` loads it. A prompt that names those tools and says to use them
directly makes the model emit a call that CANNOT succeed, and the resulting
``A tool with the name '<name>' does not exist`` is indistinguishable from a
dead server -- so the model reports the capability as missing, and that wrong
diagnosis reaches the user and persists in memory.

The prompt is the only lever here: the error text is emitted by the kiro-cli
chat binary, which has no deferral-aware variant, so it cannot be reworded from
this repository. These assertions pin the correct instruction against prompt
rewrites.
"""

from __future__ import annotations

from pathlib import Path

import pytest

CONFIG_DIR = Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "config"
PROMPTS = ("prompt.md", "prompt-orchestrator.md")


@pytest.mark.parametrize("name", PROMPTS)
def test_prompt_does_not_promise_direct_mcp_tool_calls(name: str) -> None:
    text = (CONFIG_DIR / name).read_text(encoding="utf-8")
    # The retired claim: it told the model the listed tools were already usable.
    assert "(use directly, never via bash)" not in text


@pytest.mark.parametrize("name", PROMPTS)
def test_prompt_explains_the_deferred_tool_error(name: str) -> None:
    text = (CONFIG_DIR / name).read_text(encoding="utf-8")
    # The model must be able to recognise the error it will actually receive.
    assert "A tool with the name" in text
    # ... and read it as deferral rather than absence.
    assert "DEFERRED, not missing" in text


@pytest.mark.parametrize("name", PROMPTS)
def test_prompt_gives_the_exact_recovery_call(name: str) -> None:
    text = (CONFIG_DIR / name).read_text(encoding="utf-8")
    # An exact tool_id is the reliable path; a keyword query can fall under the
    # match threshold and return nothing, which reinforces the wrong diagnosis.
    assert 'tool_search(tool_id="<server>::<name>")' in text
    assert "keyword `query` can score below the match threshold" in text


@pytest.mark.parametrize("name", PROMPTS)
def test_prompt_forbids_the_dead_server_conclusion(name: str) -> None:
    text = (CONFIG_DIR / name).read_text(encoding="utf-8")
    assert "Never read that error as the MCP server being down" in text
