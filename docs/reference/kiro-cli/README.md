# Kiro CLI Documentation

Offline mirror of the `kiro-cli` documentation, taken from the **2.x** line.
Nothing constrains the CLI to that line at runtime: `acp/client.py` launches
whichever `kiro-cli` is on PATH, and `kiro_prerequisite.py`'s
`KIRO_CLI_UPDATE_COMMAND` is an unversioned `kiro-cli update`. The only version
number in the code is a feature floor — `mcp_hot_reload.py`'s
`MCP_HOT_RELOAD_MIN_KIRO_CLI_VERSION` of 2.21.0, which decides whether MCP hot
reload is available and which a 3.x CLI also satisfies. So these pages describe
the line they were fetched from, not a supported-version contract. Content here
reflects fetches through 2026-09.

Upstream re-organised its information architecture: **feature** pages moved out of
`/docs/cli/` to top level (`/docs/skills/`, `/docs/hooks/`), while
**CLI-specific** pages such as `/docs/cli/acp/` did not. The Source column below
records the path each page is fetched from.

Two pages are **local, not mirrored**, and the tree's do-not-author rule does not
reach them — [`../README.md`](../README.md) names the exception mechanism:

- `chat/voice.md` — Kiro Crew's own voice setup, with no upstream counterpart.
- `mcp/oauth-token-storage.md` — derived from the `aws/amazon-q-developer-cli`
  source, not from published docs.

`acp.md` and `hooks.md` carry local additions on top of mirrored content; each is
marked in the page. A re-fetch must preserve them.

## Contents

| File | Source |
|------|--------|
| [installation.md](installation.md) | /docs/cli/installation/ |
| [authentication.md](authentication.md) | /docs/cli/authentication/ |
| [chat/](chat/) | /docs/cli/chat/ |
| [chat/model-selection.md](chat/model-selection.md) | /docs/cli/chat/model-selection/ — reasoning-effort behaviour is documented first-party and more precisely in [providers](../../system-specs/modules/providers.md) |
| [chat/session-management.md](chat/session-management.md) | /docs/cli/chat/session-management/ |
| [chat/subagents.md](chat/subagents.md) | /docs/cli/chat/subagents/ — describes **upstream's** subagent model, not Kiro Crew's; [subagent](../../system-specs/modules/subagent.md) owns the shipped `spawn_run` / `spawn_continue` contract |
| [chat/voice.md](chat/voice.md) | **Not a mirror**, and the one page here with no upstream source: Kiro Crew's own voice setup, speech-to-text in (local by default) and text-to-speech out (local Piper, or Amazon Polly). Its behavioral contracts live in [stt-streaming](../../system-specs/modules/stt-streaming.md) and [voice-streaming](../../system-specs/modules/voice-streaming.md); its settings reference is [configuration](../../../src/kiro_crew/docs/configuration.md). |
| [steering.md](steering.md) | /docs/steering/ |
| [skills.md](skills.md) | /docs/skills/ |
| [hooks.md](hooks.md) | /docs/hooks/ — plus Kiro Crew's fail-closed `PreToolUse` exit contract, which has no upstream counterpart |
| [autocomplete.md](autocomplete.md) | /docs/cli/autocomplete/ |
| [custom-agents/](custom-agents/) | /docs/cli/custom-agents/ |
| [custom-agents/creating.md](custom-agents/creating.md) | /docs/cli/custom-agents/creating/ |
| [custom-agents/configuration-reference.md](custom-agents/configuration-reference.md) | /docs/cli/custom-agents/configuration-reference/ — the agent-spec schema `acp/kas_agents.py`'s `UNSUPPORTED_SPEC_KEYS` is measured against |
| [acp.md](acp.md) | /docs/cli/acp/ — plus local rows for `session/set_config_option` and `_session/terminate`, which upstream's method table does not carry |
| [mcp/](mcp/) | /docs/cli/mcp/ |
| [mcp/configuration.md](mcp/configuration.md) | /docs/cli/mcp/configuration/ |
| [mcp/examples.md](mcp/examples.md) | /docs/cli/mcp/examples/ — [connecting a remote OAuth MCP server](../../guides/connecting-remote-oauth-mcp-server.md) is the first-party guide readers are sent to |
| [mcp/oauth-token-storage.md](mcp/oauth-token-storage.md) | **Not a mirror**: derived from the `aws/amazon-q-developer-cli` source |
| [mcp/security.md](mcp/security.md) | /docs/cli/mcp/security/ — Kiro Crew's own security model is [security](../../system-specs/modules/security.md), which governs |
| [reference/slash-commands.md](reference/slash-commands.md) | /docs/cli/reference/slash-commands/ — `scripts/docs_lint.py` hand-excepts this directory from the per-directory-index rule, so moving or renaming it requires editing that exception in the same change |
