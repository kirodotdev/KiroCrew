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
| [crew-members.md](crew-members.md) | Named crewmates, their standing DM threads, and routing work to one with `select_crew` / `route_crew`. |
| [agent-spec-fields.md](agent-spec-fields.md) | Every agent-spec field, what it does, and how that differs per ACP backend. |
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
| [issue-radar-pipeline.md](issue-radar-pipeline.md) | Which step of automated triage every issue is sitting in, how long it has been there, and what each agent session cost. |
| [agent-questions.md](agent-questions.md) | Letting an agent pause mid-turn to ask a clickable question. |
| [followup-suggestions.md](followup-suggestions.md) | Agent-proposed next steps above the composer. |
| [feature-tips.md](feature-tips.md) | Personalized tips pointing at features you have not used. |
| [feature-videos.md](feature-videos.md) | Short intro clips for features this install has not used yet. |
| [inbound-webhooks.md](inbound-webhooks.md) | Letting external systems trigger an agent turn over HTTP. |
| [deploy-web.md](deploy-web.md) | Publishing artifacts to a public HTTPS URL on your own AWS. |
| [snapshot-and-restore.md](snapshot-and-restore.md) | Backing up and restoring Kiro Crew state. |
| [workflows.md](workflows.md) | Multi-phase agent runs you can watch, restart in part, and save for reuse. |
| [secrets-vault.md](secrets-vault.md) | Storing credentials encrypted where the agent cannot read them. |
| [monitor-loops.md](monitor-loops.md) | Keeping one session checking something on an interval until an exit condition fires. |
| [session-ledger.md](session-ledger.md) | The durable per-session work record that survives context compaction. |
| [session-control.md](session-control.md) | Opening, seeding, watching, stopping and closing another session, and the sidebar folders and tags that file them. |
| [artifacts.md](artifacts.md) | Saving, versioning, and reverting generated UI and documents. |
| [computer-use.md](computer-use.md) | Reading and driving native desktop applications; opt-in and off by default. |
| [decisions.md](decisions.md) | Jev decisions: letting a small fast model pick the automatic skill for a sampled conversation and pick whether a mid-turn message steers or queues, with the shipped behaviour as the fallback; flagging a risky tool call on its own card in a session that approves its own calls, which changes no permission; and a basic diagnostic log for all of them. |
| [browser-control.md](browser-control.md) | Driving a real web page from the dashboard's Browser panel. |

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
| [imessage-integration.md](imessage-integration.md) | iMessage setup and behavior on a Mac that owns the Messages database. |
| [channel-capabilities.md](channel-capabilities.md) | One matrix of what every channel can do: streaming, buttons, uploads, reply length, approval timeout. |

## Platform

| Doc | Covers |
|---|---|
| [apps.md](apps.md) | The apps that ship in the package, the App Store that enables and updates them, and what enabling one grants. |
| [mcp-apps.md](mcp-apps.md) | Rendering interactive MCP tool output in chat: the two gates, what a server declares, and the plain-text fallback. |
| [connections.md](connections.md) | MCP servers and OAuth'd services: the curated catalogue, adding your own, credential custody, health badges, tool renames, and quarantine. |
| [settings-deeplink.md](settings-deeplink.md) | Answering "where is that setting?" with a link that opens and flashes the control, and the generated registry it comes from. |

## Maintaining this directory

Three constraints make this tree different from `docs/`:

- **Filenames are an API.** `tips.py` globs `*.md` here and filters through
  `tips_allowlist.py`, extracting each doc's H1 and first paragraph into the in-app
  feature catalog; dashboard Settings panels hardcode GitHub URLs to specific
  filenames; and a test pins one name. Renaming or deleting a file here is a code
  change, and a doc dropped from the catalog fails silently. `scripts/docs-lint.sh`
  pins the coupled names.
- **The tree is flat, deliberately.** `setup.cfg`'s `package_data` glob for this
  directory does not recurse, so a file in a subdirectory would ship in the sdist
  but be missing from the wheel.
- **Not every file here is prose.** `settings-registry.generated.json` is a build
  artifact of the dashboard (`npm run gen:settings`), shipped beside
  [settings-deeplink.md](settings-deeplink.md) so the agent can enumerate the
  Settings controls at runtime. Do not hand-edit it; a frontend test byte-matches
  it against the live panels.

Because every doc here reaches every user, keep the content task-oriented and free
of internal design narration. An engineering note belongs in
[`../../../docs/`](../../../docs/README.md) instead — for example
[how the model's context is assembled](../../../docs/architecture/context-management.md),
which cites private symbols and so lives there rather than here. Each doc's first paragraph is
read verbatim as a feature description, so write it to stand alone.
