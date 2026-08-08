# Module specs

One spec per backend subsystem. **These are change-control contracts:** read the
spec for the subsystem you are touching before you change it, and update it in the
same commit when you change what it documents.

This is also the on-demand load target for an AI session. The routing table in
[`../../../AGENTS.md`](../../../AGENTS.md) maps a subsystem to its spec here, so an
agent loads only the one it needs.

## Core runtime

| Spec | Subsystem |
|---|---|
| [acp-client.md](acp-client.md) | The ACP JSON-RPC client that drives `kiro-cli`: transport, framing, timeouts, and the backend seam. |
| [providers.md](providers.md) | The `LLMProvider` interface and the KiroACP-only provider surface. |
| [session.md](session.md) | Sessions, slots, session keys, the warm pool, and PID tracking. |
| [history.md](history.md) | Conversation persistence, JSONL rotation, and transcript search. |
| [session-storage.md](session-storage.md) | What sessions cost on disk, and the user-initiated trash that reclaims it. |
| [config.md](config.md) | The config schema, defaults, loading, and live reload. |
| [cli.md](cli.md) | Every CLI command, the gateway flags, and the test harness. |
| [heartbeat.md](heartbeat.md) | The liveness heartbeat and its restricted tool allowlist. |
| [metrics.md](metrics.md) | Duration histograms, system metrics, and the loop-stall watchdog. |
| [sel.md](sel.md) | The security event log: what is audited and how it is signed. |

## Security and platform

| Spec | Subsystem |
|---|---|
| [security.md](security.md) | Sensitive paths, denied commands, credential redaction, the sandbox, and the keystone. |
| [governance.md](governance.md) | The two-level governance model, the scope catalog, and the PreToolUse gate. |
| [platform-context.md](platform-context.md) | The Composed Platform Providers seam, edition resolution, and signed-plugin admission. |
| [computer-use.md](computer-use.md) | Native desktop GUI automation, its keystone opt-in, and the in-band refusals. |

## Agents and orchestration

| Spec | Subsystem |
|---|---|
| [subagent.md](subagent.md) | Spawning background workers, result delivery, and orphan recovery. |
| [task.md](task.md) | Task models and state. |
| [taskrunner.md](taskrunner.md) | The execution engine that runs a task spec to completion. |
| [workflows.md](workflows.md) | The dynamic-workflow engine: the frozen `ctx` contract, the event stream, and budgets. |
| [workflow-gates.md](workflow-gates.md) | The named conformance gates the workflow engine asserts, and the test pinning each. |
| [autopilot.md](autopilot.md) | Plan-driven orchestration and its lifecycle. |
| [persistent-agent-channels.md](persistent-agent-channels.md) | Long-lived channels for multi-agent collaboration. |
| [channel-history.md](channel-history.md) | The channel history buffer. |

## Memory and knowledge

| Spec | Subsystem |
|---|---|
| [memory-skills-hooks.md](memory-skills-hooks.md) | The memory layers, embeddings, lessons, skills, and hooks. |
| [knowledge.md](knowledge.md) | The knowledge graph and local knowledge search. |
| [onboarding-import.md](onboarding-import.md) | Importing existing content at onboarding, and its embedding cost. |
| [learn-cron-dashboard.md](learn-cron-dashboard.md) | Lessons, cron scheduling, and the dashboard handlers that expose them. |

## Channels and messaging

| Spec | Subsystem |
|---|---|
| [messaging.md](messaging.md) | The channel-neutral contracts: approvals, streaming, the mid-turn queue, and cooperative cancel. |
| [slack-gateway.md](slack-gateway.md) | The Slack gateway, its event dispatch, and Block Kit rendering. |

## Apps and UI surfaces

| Spec | Subsystem |
|---|---|
| [app-kit-platform.md](app-kit-platform.md) | App contracts: MCP scoping, agent JSON composition, permissions, and dependencies. |
| [mcp-apps.md](mcp-apps.md) | Apps that surface as MCP servers. |
| [artifacts.md](artifacts.md) | Artifact identity, versioning, and the companion chat panel. |
| [themes.md](themes.md) | The theme tier model and the CSS variable contract. |
| [md-notebook.md](md-notebook.md) | The inline markdown viewer and editor. |
| [side.md](side.md) | The chat side panel. |
| [browser.md](browser.md) | Website browsing through Playwright MCP. |

## Built-in apps

| Spec | Subsystem |
|---|---|
| [papyrus.md](papyrus.md) | The Papyrus writing app. |
| [pptx-maker.md](pptx-maker.md) | Deck generation. |
| [meetings.md](meetings.md) | Meeting capture and summarization. |
| [issue-radar.md](issue-radar.md) | Issue triage and grouping. |
| [ops-mission-control.md](ops-mission-control.md) | Autonomous ops first responder: alarms, pages and monitors. |
| [mochi.md](mochi.md) | The Mochi app. |
| [auto-improvement.md](auto-improvement.md) | Measurement-first self-improvement loop: ruler calibration, keep-or-revert cycles, draft PRs. |
| [auto-improvement-test-plan.md](auto-improvement-test-plan.md) | Integration test plan for auto-improvement (all endpoints + UI + full loop), against a real GitHub repo. |

## Operations

| Spec | Subsystem |
|---|---|
| [cloud.md](cloud.md) | Cloud connect and remote gateway login. |
| [instances.md](instances.md) | Managing multiple instances over SSH. Sections here are cited by number from `cloud/connect.py`, so do not renumber them. |
| [dev-fleet.md](dev-fleet.md) | Worktree fleet management and pruning. |
