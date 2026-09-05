# Kiro Crew user documentation

**These docs ship inside the Python package.** They are the end-user documentation:
in-app reading, dashboard Settings links, and the feature-tips catalog all resolve
here. Contributor and architecture docs live in [`../../../docs/`](../../../docs/README.md).

[index.md](index.md) is the in-app entry point. This README is the same set,
organized for someone browsing the repository.

## Getting started

| Doc | Covers |
|---|---|
| [getting-started.md](getting-started.md) | Install, first run, and background operation. |
| [configuration.md](configuration.md) | Config file reference, environment variables, and sandbox modes. |
| [use-cases.md](use-cases.md) | Real-world workflows. |
| [troubleshooting.md](troubleshooting.md) | Common problems and fixes. |
| [blocked-commands.md](blocked-commands.md) | Why a command was refused, what the agent is told to do instead, and how to check your credential setup. |

## Core capabilities

| Doc | Covers |
|---|---|
| [agents.md](agents.md) | Switching between specialized agents per conversation, thread, or cron job. |
| [skills.md](skills.md) | Drop-in markdown knowledge packs for domain-specific workflows. |
| [monitoring.md](monitoring.md) | Token-efficient pull-request monitoring and finite legacy fallbacks. |
| [cron-and-scheduling.md](cron-and-scheduling.md) | Scheduling recurring tasks. |
| [subagents.md](subagents.md) | Spawning parallel background workers for fan-out work. |
| [dynamic-subagent-sizing.md](dynamic-subagent-sizing.md) | How the concurrent sub-agent cap is sized from host memory and CPU. |
| [task-runner.md](task-runner.md) | Autonomous multi-step execution from a spec file. |
| [research-lab.md](research-lab.md) | Multi-cycle research campaigns with exportable reports. |
| [memory-and-learning.md](memory-and-learning.md) | Persistent preferences, project context, and learned corrections. |
| [knowledge-library-how-it-works.md](knowledge-library-how-it-works.md) | How the knowledge graph is built from your documents. |
| [dashboard.md](dashboard.md) | The web dashboard: multi-session chat, memory management, live metrics. |
| [agent-questions.md](agent-questions.md) | Letting an agent pause mid-turn to ask a clickable question. |
| [followup-suggestions.md](followup-suggestions.md) | Agent-proposed next steps above the composer. |
| [feature-tips.md](feature-tips.md) | Personalized tips pointing at features you have not used. |
| [inbound-webhooks.md](inbound-webhooks.md) | Letting external systems trigger an agent turn over HTTP. |
| [deploy-web.md](deploy-web.md) | Publishing artifacts to a public HTTPS URL on your own AWS. |
| [snapshot-and-restore.md](snapshot-and-restore.md) | Backing up and restoring Kiro Crew state. |

## Channels

| Doc | Covers |
|---|---|
| [slack-integration.md](slack-integration.md) | Slack DMs, tool approval, streaming, channel monitoring. |
| [discord-integration.md](discord-integration.md) | Discord setup and behavior. |
| [telegram-integration.md](telegram-integration.md) | Telegram setup and behavior. |
| [teams-integration.md](teams-integration.md) | Microsoft Teams setup, including Azure Bot registration. |
| [webex-integration.md](webex-integration.md) | Webex setup and behavior. |
| [wecom-integration.md](wecom-integration.md) | WeCom setup and behavior. |
| [weixin-integration.md](weixin-integration.md) | Weixin setup, and the risks to read first. |
| [whatsapp-integration.md](whatsapp-integration.md) | WhatsApp (QR-linked personal account) setup, and the risks to read first. |
| [feishu-integration.md](feishu-integration.md) | Feishu (Lark/飞书) setup and behavior. |
| [messaging-transport.md](messaging-transport.md) | The channel-neutral contracts every transport shares. |

## Platform

| Doc | Covers |
|---|---|
| [app-platform-trust-model.md](app-platform-trust-model.md) | Enabled apps run in-process with full privileges: the trust boundary and its audit. |
| [mcp-apps.md](mcp-apps.md) | Rendering interactive MCP tool output in chat: the two gates, what a server declares, and the plain-text fallback. |
| [dashboard-iframe-hosts.md](dashboard-iframe-hosts.md) | The four iframe hosts, their differing sandboxes, and why they are not interchangeable. |

## Maintaining this directory

Two constraints make this tree different from `docs/`:

- **Filenames are an API.** `tips.py` globs `*.md` here and filters through
  `tips_allowlist.py`, extracting each doc's H1 and first paragraph into the in-app
  feature catalog; dashboard Settings panels hardcode GitHub URLs to specific
  filenames; and a test pins one name. Renaming or deleting a file here is a code
  change, and a doc dropped from the catalog fails silently. `scripts/docs-lint.sh`
  pins the coupled names.
- **The tree is flat, deliberately.** `setup.cfg`'s `package_data` glob for this
  directory does not recurse, so a file in a subdirectory would ship in the sdist
  but be missing from the wheel.

Because every doc here reaches every user, keep the content task-oriented and free
of internal design narration. An engineering note belongs in
[`../../../docs/`](../../../docs/README.md) instead. Each doc's first paragraph is
read verbatim as a feature description, so write it to stand alone.
