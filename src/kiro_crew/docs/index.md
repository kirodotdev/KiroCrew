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
| [Monitoring](monitoring.md) | Watch a public GitHub pull request with zero-turn unchanged probes and bounded action wakes |
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

## Chat Channels

Besides the dashboard and CLI, Kiro Crew ships channel integrations for
[Slack](slack-integration.md), [Discord](discord-integration.md),
[Telegram](telegram-integration.md), [Teams](teams-integration.md),
[Webex](webex-integration.md), [WeCom](wecom-integration.md),
[Weixin](weixin-integration.md), [iMessage](imessage-integration.md),
[WhatsApp](whatsapp-integration.md), and
[Feishu](feishu-integration.md). They
share one channel-neutral core, described
in [Messaging Transport](messaging-transport.md).

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
- [Dashboard iframe hosts](dashboard-iframe-hosts.md): which of the four embed
  hosts to use, and why their sandboxes differ

## Security

- OS-level sandbox for the agent process, layered on top of kiro-cli's own
- Credential redaction across every LLM output path
- HMAC-SHA256 signed, IP-pinned dashboard tokens
- Denied-command rules enforced at Kiro Crew's own PreToolUse gate, with audit
  logging, and a refusal that names the sanctioned path instead of only the rule
  ([Blocked commands](blocked-commands.md))
- Prompt-injection credential-exfiltration protection
- Slack access is owner-only: multi-user access and open channels are refused
- [App Platform Trust Model](app-platform-trust-model.md): enabled apps run
  in-process with full privileges; the trust boundary and its audit

## Links

- [Repository](https://github.com/kirodotdev/KiroCrew): source, issues, and
  feature requests. `CONTRIBUTING.md` in the repository root has the
  contribution guidelines.
