# Kiro Crew Documentation

Kiro Crew is a personal, autonomous AI agent that runs locally on your own
machine. It is powered by kiro-cli (KiroACP) and reaches tools over the Model
Context Protocol (MCP). Everything below is the reference for the features you
can reach from the dashboard, the CLI, or a connected messaging channel.

## Quick Start

Install the prebuilt, signed wheel:

```bash
curl -fsSL https://download.crew.kiro.dev/cli.sh | sh
```

Then start the gateway and open the dashboard:

```bash
kirocrew gateway     # → http://localhost:5476
```

`kiro-cli` must be installed, on your `PATH`, and logged in. See
[Getting Started](getting-started.md) for the source install, the pip channel
index, first-time setup, and connecting messaging channels.

## Core Capabilities

| Capability | Description |
|------------|-------------|
| [Cron Jobs](cron-and-scheduling.md) | Schedule recurring tasks, e.g. "every weekday at 9am give me a pipeline briefing" |
| [Subagents](subagents.md) | Spawn parallel background workers for fan-out research and multi-package work |
| [Dynamic Sub-Agent Sizing](dynamic-subagent-sizing.md) | Auto-size the concurrent sub-agent cap from host memory/CPU and a learned per-agent cost |
| [Memory](memory-and-learning.md) | Persistent preferences, project context, and learned corrections across sessions, plus per-session persistent / incognito / temporary memory modes |
| [Task Runner](task-runner.md) | Autonomous multi-step execution from spec files: hand it a task, walk away |
| [Research Lab](research-lab.md) | Autonomous multi-cycle research campaigns with scoping, adaptive agent execution, and exportable reports |
| [Dashboard](dashboard.md) | React web UI with multi-session chat, memory management, and live system metrics |
| [Agent Questions](agent-questions.md) | Let an agent pause mid-turn and ask you a clickable multiple-choice question |
| Chat Channels | DM-based chat with tool approval — [Slack](slack-integration.md), [Discord](discord-integration.md), [Telegram](telegram-integration.md), [Teams](teams-integration.md), [Webex](webex-integration.md), [WeCom](wecom-integration.md), [WeChat](weixin-integration.md), [iMessage](imessage-integration.md), [WhatsApp](whatsapp-integration.md), [Feishu](feishu-integration.md); per-channel capabilities in each guide |
| [Agents](agents.md) | Switch between specialized agents per conversation, thread, or cron job |
| [Skills](skills.md) | Drop-in markdown knowledge packs for domain-specific workflows |
| [Dynamic Workflows](workflows.md) | Multi-phase agent orchestration authored from a plain-language goal: watch a run, restart part of it from cache, save it for reuse |
| [Artifacts](artifacts.md) | Save, version, and revert generated UI and documents so they outlive the chat scrollback |
| [Monitor Loops](monitor-loops.md) | Keep one session checking something on an interval — a pull request, a CI run, a deployment — until an exit condition fires |
| [Session Ledger](session-ledger.md) | A durable per-session record of goal, phase, and next step that survives context compaction |
| [Browser Control](browser-control.md) | Drive a real web page from the dashboard's Browser panel: navigate, snapshot, click, type, screenshot |
| [Computer Use](computer-use.md) | Read and drive native desktop applications through the accessibility layer; opt-in and off by default |

## Additional Features

| Feature | Description |
|---------|-------------|
| [Backup & Restore](snapshot-and-restore.md) | Portable snapshot and restore of Kiro Crew state, for upgrades and machine migration |
| [Knowledge Library](knowledge-library-how-it-works.md) | Semantic search over your own documents, folders, and generated artifacts |
| [Web Deploy](deploy-web.md) | Publish artifacts to a public HTTPS URL on your own AWS (private S3 + CloudFront + OAC) |
| [Inbound Webhooks](inbound-webhooks.md) | Let an external system trigger an agent turn over HTTP — named tokens, HMAC request signing, a reversible off switch, ephemeral sessions, `register_hook` resume context |
| [Feature Tips](feature-tips.md) | Occasional personalized tips above the composer pointing at features you have not used yet |
| [Follow-up Suggestions](followup-suggestions.md) | Agent-proposed next steps above the composer: start in a new git worktree, add to this session, or skip |
| [Queued-Message Editing](dashboard.md) | Edit, reorder, or cancel a chat message waiting in the queue before it runs |
| [Cooperative Stop](dashboard.md) | Stop sends a cancel first and only hard-kills after a budget, so session state survives |
| [Streaming Speech-to-Text](configuration.md) | Live transcription partials in the dashboard input, with local Whisper or optional AWS Transcribe |
| [Warm Pool](configuration.md) | Keep kiro-cli processes pre-spawned so a new session starts instantly |
| [Secrets Vault](secrets-vault.md) | Credentials encrypted on disk and refused to the agent, with a `secret://` reference left in `.env` |

## Settings reference

Settings is organized into tabs. Several have a full guide; the rest are
documented by their own in-panel help.

| Tab | Covers | Guide |
|---|---|---|
| Overview | Gateway status and the settings you change most | — |
| Imports | Bringing configuration in from another install | [Snapshot and restore](snapshot-and-restore.md) |
| Chat | Composer behaviour, queued messages, feature tips | [Feature Tips](feature-tips.md) |
| Display | Theme, language, and layout | — |
| Voice | Speech-to-text and spoken replies | [Configuration](configuration.md) |
| Notifications | Where a proactive message is delivered | — |
| Shortcuts | Keyboard bindings | — |
| Skills | Whether sessions auto-generate skills, and whether a generated one needs your approval; installed skills live under Agent Capabilities | [Skills](skills.md) |
| Channels | Per-channel setup and access control | [Channel capabilities](channel-capabilities.md) |
| Browser | Installing the browser engine and the attach token | [Browser control](browser-control.md) |
| Computer Use | Driving native desktop apps; off by default | [Computer use](computer-use.md) |
| Webhooks | Inbound tokens and request signing — hidden unless you enable it under Feature Previews | [Inbound webhooks](inbound-webhooks.md) |
| Instances | Additional gateways this dashboard can reach | — |
| Privacy | What leaves the host | [Snapshot and restore](snapshot-and-restore.md) |
| Security | The sandbox, denied commands, and the audit log | [Blocked commands](blocked-commands.md) |
| Secrets | The encrypted credential vault | [Secrets vault](secrets-vault.md) |
| Developer | The Developer Mode consent switch, plus an optional local-gateway toggle; turning it on adds a separate Developer page that holds logs, metrics, storage and the rest | [Dashboard](dashboard.md) |
| Releases | Update channel and version | [Getting Started](getting-started.md) |
| About | Version and links | — |

## Chat Channels

Besides the dashboard and CLI, Kiro Crew ships channel integrations for
[Slack](slack-integration.md), [Discord](discord-integration.md),
[Telegram](telegram-integration.md), [Teams](teams-integration.md),
[Webex](webex-integration.md), [WeCom](wecom-integration.md),
[Weixin](weixin-integration.md), [iMessage](imessage-integration.md),
[WhatsApp](whatsapp-integration.md), and
[Feishu](feishu-integration.md). They
all share one channel-neutral core, so a capability a channel lacks degrades
gracefully rather than failing the turn.

## Guides

- [Getting Started](getting-started.md): installation, first-time setup, running in the background
- [Configuration](configuration.md): config file reference, environment variables, sandbox
- [Use Cases](use-cases.md): real-world workflows from the community
- [Troubleshooting](troubleshooting.md): common issues and fixes
- [Blocked commands and credential access](blocked-commands.md): why a command was
  refused, what the agent is told to do instead, and how to check your AWS or SSO
  credential setup
- [MCP Apps](mcp-apps.md): render interactive MCP tool output (diagrams, viewers,
  forms) in chat, the two gates that enable it, what a server must declare, and why
  output stays plain text otherwise

## Security

- OS-level sandbox for the agent process, layered on top of kiro-cli's own
- Credential redaction across every LLM output path
- HMAC-SHA256 signed, IP-pinned dashboard tokens
- Denied-command rules enforced at Kiro Crew's own PreToolUse gate, with audit
  logging, and a refusal that names the sanctioned path instead of only the rule
  ([Blocked commands](blocked-commands.md))
- Prompt-injection credential-exfiltration protection
- Slack access is owner-only: multi-user access and open channels are refused
- An enabled app runs in-process with the gateway's own privileges, so enabling one
  is a trust decision. Third-party app execution is deny-by-default, and every app
  load is audited

## Links

- [Repository](https://github.com/kirodotdev/KiroCrew): source, issues, and
  feature requests. `CONTRIBUTING.md` in the repository root has the
  contribution guidelines.
