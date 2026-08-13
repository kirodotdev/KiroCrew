"""Canonical filenames of the agent configs KiroCrew generates.

Single source of truth for the on-disk names KiroCrew writes into
``~/.kiro/agents/`` (kiro specs) and ``~/.claude/agents/`` (the Claude Code MCP
sidecar). This is a **leaf module** (no intra-package imports) so both
``agent.py`` (which writes these files) and ``browser/setup.py`` (whose
Playwright convergence sweep must touch only KiroCrew-owned files) can import it
without an import cycle — ``agent.py`` imports ``converge_playwright_servers``
from ``browser/setup.py``, so ``browser/setup.py`` cannot import ``agent.py``.

Keeping the names here — rather than duplicating them as a literal in each
consumer — means adding a new managed agent spec is a one-line change in ONE
place: add its filename to ``OWNED_KIRO_AGENT_FILES`` and every consumer
(including the boot-time self-heal sweep) picks it up.
"""

from __future__ import annotations

# The primary KiroCrew agent spec.
AGENT_FILENAME = "kirocrew.json"

# Background/auxiliary managed agent specs KiroCrew writes under ~/.kiro/agents/.
LITE_AGENT_FILENAME = "kirocrew-lite.json"
KNOWLEDGE_AGENT_FILENAME = "kirocrew-knowledge.json"
RESEARCH_AGENT_FILENAME = "kirocrew-research.json"
HEARTBEAT_AGENT_FILENAME = "kirocrew-heartbeat.json"

# The Claude Code MCP sidecar filename under ~/.claude/agents/.
CC_MCP_SIDECAR_FILENAME = "kirocrew.mcp.json"

# Collective allowlists — the EXACT filenames KiroCrew owns in each dir. Used by
# the Playwright convergence sweep (browser/setup.py) so it rewrites only files
# KiroCrew generates, never a user's own agent config that happens to share a
# prefix (e.g. a hand-authored ``kirocrew-custom.json``).
OWNED_KIRO_AGENT_FILES = (
    AGENT_FILENAME,
    LITE_AGENT_FILENAME,
    KNOWLEDGE_AGENT_FILENAME,
    RESEARCH_AGENT_FILENAME,
    HEARTBEAT_AGENT_FILENAME,
)
OWNED_CC_AGENT_FILES = (CC_MCP_SIDECAR_FILENAME,)

# The specs that MUST exist for the product to work at all. kiro-cli resolves an
# agent by reading ``<agents dir>/<name>.json``; with the file absent it answers
# every ``session/set_mode`` with "Mode '<name>' not found", so a missing entry
# here fails EVERY turn rather than degrading one feature:
#   * ``kirocrew.json``      — the agent behind user-facing chat.
#   * ``kirocrew-lite.json`` — the cheap background agent (auto-titles,
#     compaction, heartbeat), reached via ``SessionManager.get_bg_session``.
# The remaining OWNED_KIRO_AGENT_FILES entries are deliberately excluded: their
# installers in ``agent.py`` already degrade to ``logger.debug`` on failure
# because each one only disables its own feature (Knowledge extraction, Research
# Lab, unattended heartbeat polling).
REQUIRED_KIRO_AGENT_FILES = (
    AGENT_FILENAME,
    LITE_AGENT_FILENAME,
)
