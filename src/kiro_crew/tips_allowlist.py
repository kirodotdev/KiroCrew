"""Explicit allowlist of user-facing docs eligible for the tips catalog.

Single source of truth shared by the runtime fallback scanner
(``kiro_crew.tips._scan_docs_catalog``) and the release-time generator
(``scripts/generate_tips_catalog.py``).

Only USER-FACING feature docs belong here. Internal architecture notes,
incident write-ups, and design documents must NOT be listed — the exploration
blend picks uniformly from the catalog, so anything listed can surface as a
user-facing tip (Codex round-7: internal MCP-gateway/incident docs were being
offered as feature tips).

Deliberately dependency-free so the maintainer script can import it without
pulling in the full kiro_crew runtime.
"""

from __future__ import annotations

TIP_DOC_ALLOWLIST: frozenset[str] = frozenset(
    {
        "agents.md",
        "configuration.md",
        "cron-and-scheduling.md",
        "dashboard.md",
        "deploy-web.md",
        "dynamic-subagent-sizing.md",
        "feature-tips.md",
        "getting-started.md",
        "knowledge-library-how-it-works.md",
        "memory-and-learning.md",
        "research-lab.md",
        "skills.md",
        "slack-integration.md",
        "subagents.md",
        "task-runner.md",
        "telegram-integration.md",
        "discord-integration.md",
        "use-cases.md",
        "webex-integration.md",
        "wechat-integration.md",
    }
)
