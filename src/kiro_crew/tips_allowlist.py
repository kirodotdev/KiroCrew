"""Explicit allowlist of user-facing docs eligible for the tips catalog.

Single source of truth shared by the runtime fallback scanner
(``kiro_crew.tips._scan_docs_catalog``) and the release-time generator
(``scripts/generate_tips_catalog.py``).

Only USER-FACING feature docs belong here. Internal architecture notes,
incident write-ups, and design documents must NOT be listed — the exploration
blend picks uniformly from the catalog, so anything listed can surface as a
user-facing tip.

Onboarding docs do not belong here either. Tips are feature DISCOVERY —
"a feature you have not used yet" (see feature-tips.md and the generation
prompt in tips.py) — and they render above the chat composer of a running
dashboard session. Anyone who can see a tip has already installed, started,
and opened Kiro Crew, so a product-overview tip like "Getting Started" can
only ever be redundant there.

Reference tables are excluded for the same reason as onboarding docs: they answer a
question a user already has rather than announcing a feature to try.
``channel-capabilities.md`` is the current example — its first paragraph describes a
lookup table, which makes a poor "have you tried" tip.

Deliberately dependency-free so the maintainer script can import it without
pulling in the full kiro_crew runtime.
"""

from __future__ import annotations

TIP_DOC_ALLOWLIST: frozenset[str] = frozenset(
    {
        "agents.md",
        "artifacts.md",
        "browser-control.md",
        "computer-use.md",
        "configuration.md",
        "cron-and-scheduling.md",
        "dashboard.md",
        "deploy-web.md",
        "discord-integration.md",
        "dynamic-subagent-sizing.md",
        "feature-tips.md",
        "imessage-integration.md",
        "feishu-integration.md",
        "knowledge-library-how-it-works.md",
        "mcp-apps.md",
        "memory-and-learning.md",
        "monitor-loops.md",
        "research-lab.md",
        "secrets-vault.md",
        "session-ledger.md",
        "skills.md",
        "slack-integration.md",
        "snapshot-and-restore.md",
        "subagents.md",
        "task-runner.md",
        "teams-integration.md",
        "telegram-integration.md",
        "use-cases.md",
        "webex-integration.md",
        "wecom-integration.md",
        "workflows.md",
        "weixin-integration.md",
        "whatsapp-integration.md",
    }
)
