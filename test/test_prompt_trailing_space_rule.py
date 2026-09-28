"""The shipped system prompts must keep the trailing-space-before-tool rule.

``StreamRedactor`` withholds the maximal trailing run of ``_CRED_CLASS``
characters until a terminator arrives, so assistant text ending flush on ``.``
or ``:`` is not delivered to the client until the next chunk. When the next
event is a tool call, that wait is the tool-argument composition time --
measured at 36.7 s for a tool with a large argument payload, during which the
user sees a sentence one word short and no tool pill.

Whitespace is not in ``_CRED_CLASS``, so a model-emitted trailing space
terminates the run scan and the text commits immediately. The rule reads as
stray whitespace guidance and is exactly the kind of line a prompt rewrite
drops, which silently reintroduces the stall -- so it is pinned by a test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

CONFIG_DIR = Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "config"
PROMPTS = ("prompt.md", "prompt-orchestrator.md")


@pytest.mark.parametrize("name", PROMPTS)
def test_prompt_requires_trailing_space_before_tool_calls(name: str) -> None:
    text = (CONFIG_DIR / name).read_text(encoding="utf-8")
    assert "trailing space before you invoke a tool" in text
