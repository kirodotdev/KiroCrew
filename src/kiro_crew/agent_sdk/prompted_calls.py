"""Provider-neutral launch facts for prompt-only MCP dispatch."""

from __future__ import annotations

from typing import NamedTuple


class PromptedCallLaunch(NamedTuple):
    """Whether a live session launched on a Crew-minted agent copy."""

    launched_on_crew_copy: bool
    backend: str
