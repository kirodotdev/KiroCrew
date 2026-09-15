# Kiro CLI Documentation

Offline mirror of the `kiro-cli` documentation. Nothing constrains the CLI to a
particular line at runtime: `acp/client.py` launches whichever `kiro-cli` is on
PATH, and `kiro_prerequisite.py`'s `KIRO_CLI_UPDATE_COMMAND` is an unversioned
`kiro-cli update`. The only version number in the code is a feature floor —
`mcp_hot_reload.py`'s `MCP_HOT_RELOAD_MIN_KIRO_CLI_VERSION` of 2.21.0, which
decides whether MCP hot reload is available and which a 3.x CLI also satisfies.
So these pages describe what upstream published on the fetch date recorded per
row, not a supported-version contract.

**Upstream is now on the CLI 3.0 line, and reorganised its information
architecture again.** Feature pages that were CLI-scoped are now
surface-agnostic: `/docs/cli/installation/` became
`/docs/getting-started/installation/`, `/docs/cli/custom-agents/**` became
`/docs/custom-agents/**`, `/docs/cli/mcp/**` became `/docs/mcp/**`,
`/docs/cli/reference/slash-commands/` became `/docs/reference/slash-commands/`,
`/docs/cli/chat/model-selection/` was replaced by `/docs/models/**`, and
`/docs/cli/chat/subagents/` moved to `/docs/custom-agents/subagents/`. Genuinely
CLI-specific pages — `/docs/cli/chat/`, `/docs/cli/acp/`,
`/docs/cli/autocomplete/` — stayed put. Upstream keeps the 2.x formats at
`/docs/cli/2x-reference/`. The Source column below records the path each page is
fetched from and when. Every page also carries the same pair on its own first
lines.

Every mirrored page has a Markdown twin at the same path with a `.md` suffix, so
`/docs/skills/` is fetchable as `/docs/skills.md`. Use that for a re-fetch: it
returns the page without the site's navigation chrome. `https://kiro.dev/llms.txt`
indexes every page and is the fastest way to find where one moved to.

Two pages are **local, not mirrored**, and the tree's do-not-author rule does not
reach them — [`../README.md`](../README.md) names the exception mechanism:

- `chat/voice.md` — Kiro Crew's own voice setup, with no upstream counterpart.
- `mcp/oauth-token-storage.md` — derived from the `aws/amazon-q-developer-cli`
  source, not from published docs.

`acp.md`, `hooks.md` and `steering.md` carry local additions on top of mirrored
content; each is marked in the page. A re-fetch must preserve them.

## Contents

| File | Source | Fetched |
|------|--------|---------|
| [installation.md](installation.md) | /docs/getting-started/installation/ | 2026-09-06 |
| [authentication.md](authentication.md) | /docs/getting-started/authentication/ | 2026-09-06 |
| [chat/](chat/) | /docs/cli/chat/ | 2026-09-06 |
| [chat/model-selection.md](chat/model-selection.md) | /docs/models/ and /docs/models/available-models/ — reasoning-effort behaviour is documented first-party and more precisely in [providers](../../system-specs/modules/providers.md) | 2026-09-06 |
| [chat/session-management.md](chat/session-management.md) | /docs/cli/chat/session-management/ | 2026-09-06 |
| [chat/subagents.md](chat/subagents.md) | /docs/custom-agents/subagents/ — describes **upstream's** subagent model, not Kiro Crew's; [subagent](../../system-specs/modules/subagent.md) owns the shipped `spawn_run` / `spawn_continue` contract | 2026-09-06 |
| [chat/voice.md](chat/voice.md) | **Not a mirror**, and the one page here with no upstream source: Kiro Crew's own voice setup, speech-to-text in (local by default) and text-to-speech out (local Piper, or Amazon Polly). Its behavioral contracts live in [stt-streaming](../../system-specs/modules/stt-streaming.md) and [voice-streaming](../../system-specs/modules/voice-streaming.md); its settings reference is [configuration](../../../src/kiro_crew/docs/configuration.md). | n/a |
| [steering.md](steering.md) | /docs/steering/ — plus local measurement of what kiro-cli does with `inclusion` | 2026-09-06 |
| [skills.md](skills.md) | /docs/skills/ | 2026-09-06 |
| [hooks.md](hooks.md) | /docs/hooks/ and its `types` and `actions` subpages — plus Kiro Crew's fail-closed `PreToolUse` exit contract, which has no upstream counterpart | 2026-09-06 |
| [autocomplete.md](autocomplete.md) | /docs/cli/autocomplete/ | 2026-09-06 |
| [custom-agents/](custom-agents/) | /docs/custom-agents/ | 2026-09-06 |
| [custom-agents/creating.md](custom-agents/creating.md) | /docs/custom-agents/creating/ | 2026-09-06 |
| [custom-agents/configuration-reference.md](custom-agents/configuration-reference.md) | /docs/custom-agents/configuration-reference/ — the agent-spec schema `acp/kas_agents.py`'s `UNSUPPORTED_SPEC_KEYS` is measured against | 2026-09-06 |
| [acp.md](acp.md) | /docs/cli/acp/ — plus a local row for `session/set_config_option` and the measured session-update names, which upstream's method table does not carry | 2026-09-06 |
| [mcp/](mcp/) | /docs/mcp/ | 2026-09-06 |
| [mcp/configuration.md](mcp/configuration.md) | /docs/mcp/configuration/ | 2026-09-06 |
| [mcp/examples.md](mcp/examples.md) | /docs/mcp/examples/ — [connecting a remote OAuth MCP server](../../guides/connecting-remote-oauth-mcp-server.md) is the first-party guide readers are sent to | 2026-09-06 |
| [mcp/oauth-token-storage.md](mcp/oauth-token-storage.md) | **Not a mirror**: derived from the `aws/amazon-q-developer-cli` source | n/a |
| [mcp/security.md](mcp/security.md) | /docs/mcp/security/ — Kiro Crew's own security model is [security](../../system-specs/modules/security.md), which governs | 2026-09-06 |
| [reference/slash-commands.md](reference/slash-commands.md) | /docs/reference/slash-commands/ — `scripts/docs_lint.py` hand-excepts this directory from the per-directory-index rule, so moving or renaming it requires editing that exception in the same change | 2026-09-06 |
