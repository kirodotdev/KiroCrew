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
| [providers.md](providers.md) | The `LLMProvider` interface, the `AcpProvider` the factory selects, and how a backend id is chosen. |
| [agent-host-contract.md](agent-host-contract.md) | What an agent backend must supply besides speaking ACP: agent layout, session store, identity, sandbox, MCP delivery, billing, permission engine, auxiliary runtimes — kiro-cli, KAS and Claude Code side by side, with the new-provider checklist. |
| [claude-code-provider.md](claude-code-provider.md) | Claude Code as a selectable ACP harness: the live spawn path, the two binaries it needs on the machine, and the MCP gap a Claude session still carries. |
| [harness-parity.md](harness-parity.md) | The invariants keeping the Kiro harness first-class while other harnesses are adapted, and the test pinning each. |
| [harness-onboarding.md](harness-onboarding.md) | The sequence a new ACP harness walks to land: vocabulary, capability decisions, spawn path, handshake, install probe, selectability, and what a live harness additionally touches. |
| [model-fallback.md](model-fallback.md) | The throttle-exhaustion model fallback (`agent.fallback_model`): trigger, shared walk, sticky restore, visibility. |
| [session.md](session.md) | Sessions, slots, session keys, the warm pool, and PID tracking. |
| [history.md](history.md) | Conversation persistence, JSONL rotation, and transcript search. |
| [session-summary.md](session-summary.md) | Intent-level session summaries: the sidecar cache, extraction, and the turn-end pass. |
| [session-work-ledger.md](session-work-ledger.md) | Per-session durable work state (goal, phase, tried, artifacts) on disk, its MCP tools, and monitor-loop snapshot injection. |
| [file-search.md](file-search.md) | The `@`-mention file/folder search: index, ranking, `kinds` filter, and the sensitive-path symmetry. |
| [session-storage.md](session-storage.md) | What sessions cost on disk, and the user-initiated trash that reclaims it. |
| [session-control.md](session-control.md) | One chat session opening, stopping, and reading another. |
| [config.md](config.md) | The config schema, defaults, loading, and live reload. |
| [cli.md](cli.md) | Every CLI command, the gateway flags, and the test harness. |
| [heartbeat.md](heartbeat.md) | The liveness heartbeat and its restricted tool allowlist. |
| [metrics.md](metrics.md) | Duration histograms, system metrics, and the loop-stall watchdog. |
| [sel.md](sel.md) | The security event log: what is audited and how it is signed. |

## Security and platform

| Spec | Subsystem |
|---|---|
| [security.md](security.md) | Sensitive paths, denied commands, credential redaction, the sandbox, and the keystone. |
| [dashboard-token-auth.md](dashboard-token-auth.md) | Signed, IP-pinned dashboard tokens, session TTLs, and the refresh chain. Owns the mechanism; `security.md` owns the threat model. |
| [governance.md](governance.md) | The two-level governance model, the scope catalog, and the PreToolUse gate. |
| [platform-context.md](platform-context.md) | The Composed Platform Providers seam, edition resolution, and signed-plugin admission. |
| [computer-use.md](computer-use.md) | Native desktop GUI automation, its keystone opt-in, and the in-band refusals. |
| [kas-auth.md](kas-auth.md) | KAS-mode auth: the Kiro OIDC login/refresh/storage lifecycle Kiro Crew runs itself when there is no kiro-cli. |

## Agents and orchestration

| Spec | Subsystem |
|---|---|
| [subagent.md](subagent.md) | Spawning background workers, result delivery, and orphan recovery. |
| [agent-interrupt-controller.md](agent-interrupt-controller.md) | `kiro_crew.irq`: masking, coalescing, epoch resets and an error backstop for script-cron pollers, so a cheap probe interrupts an expensive agent turn instead of the turn polling. Also the app-facing probe SDK. |
| [babysit-pr-watch.md](babysit-pr-watch.md) | Zero-token PR polling for babysit loops: a script cron that wakes the owning session only on unexpected state. |
| [task.md](task.md) | Task models and state. |
| [taskrunner.md](taskrunner.md) | The execution engine that runs a task spec to completion. |
| [workflows.md](workflows.md) | The dynamic-workflow engine: the frozen `ctx` contract, the event stream, budgets, and the named conformance gates with the test pinning each. |
| [autopilot.md](autopilot.md) | Plan-driven orchestration and its lifecycle. |
| [crew-mode.md](crew-mode.md) | Crews: the config record, `select_crew` roster and binding, model and workspace resolution, the Crews UI, and the `"crew"` slot mode's durable multi-topic control plane. |
| [pipeline-conductor.md](pipeline-conductor.md) | The `kirocrew-pipeline-conductor` agent and its skill: the generated spec's permission narrowing, the three deterministic scripts, the patrol cycle, and what the RFC leaves unbuilt. |
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
| [slack-gateway.md](slack-gateway.md) | The Slack gateway, its event dispatch, Block Kit rendering, and the `action::` inline-action value protocol. |
| [stt-streaming.md](stt-streaming.md) | Live dictation in the composer: the three providers, the WebSocket frames, the local recognizer's endpointing and partial pipeline, and the model download. |
| [voice-streaming.md](voice-streaming.md) | Streaming voice replies, and the text normalization applied before synthesis. |
| [turn-complete-chime.md](turn-complete-chime.md) | The end-of-turn audio cue, and what the policy deliberately does not inspect. |

## Apps and UI surfaces

| Spec | Subsystem |
|---|---|
| [app-kit-platform.md](app-kit-platform.md) | App contracts: MCP scoping, agent JSON composition, permissions, and dependencies. |
| [mcp-apps.md](mcp-apps.md) | Apps that surface as MCP servers. |
| [mcp-shareability.md](mcp-shareability.md) | Predicting which MCP servers can share one backend, from local evidence. |
| [mcp-gateway-backend-replacement.md](mcp-gateway-backend-replacement.md) | Validating a replacement MCP backend's tool set before a live session adopts it. |
| [mcp-probe-quarantine.md](mcp-probe-quarantine.md) | A durable consecutive-probe-failure count per MCP server, surfaced on its dashboard row with a reset control. The unmount half is deferred; the spec records why. |
| [app-notifications.md](app-notifications.md) | How an app publishes a notification to the local bus, and the two shipped producers. |
| [artifacts.md](artifacts.md) | Artifact identity, versioning, and the companion chat panel. |
| [prompt-optimizer.md](prompt-optimizer.md) | Rewriting a draft prompt on demand, and the paste-forwarding surface. |
| [steering-viewer.md](steering-viewer.md) | Reading, creating, editing and deleting the steering files a session loads, and the declared-`inclusion` reporting. |
| [turn-stats-footer.md](turn-stats-footer.md) | The per-turn token and timing footer: capture, persistence, and the frontend render gates. |
| [workflow-chat-cards.md](workflow-chat-cards.md) | Rendering a workflow run's progress as a chat card. |
| [themes.md](themes.md) | The theme tier model and the CSS variable contract. |
| [md-notebook.md](md-notebook.md) | The inline markdown viewer and editor. |
| [side.md](side.md) | The chat side panel. |
| [browser.md](browser.md) | Website browsing through the `playwright-cli` shell commands. |

## Built-in apps

| Spec | Subsystem |
|---|---|
| [papyrus.md](papyrus.md) | The Papyrus writing app. |
| [aws-control.md](aws-control.md) | The AWS account portal and S3-backed cloud drive app: accounts, Drive/Library/Backup, consent and confirmation guards, sharing. |
| [command-bar.md](command-bar.md) | The opt-in launcher that replaces quick-search: the overlay seam, the request-free root, ranking and scopes. |
| [pptx-maker.md](pptx-maker.md) | Deck generation. |
| [meetings.md](meetings.md) | Meeting capture and summarization. |
| [issue-radar.md](issue-radar.md) | Issue triage and grouping. |
| [ops-mission-control.md](ops-mission-control.md) | Autonomous ops first responder: alarms, pages and monitors. |
| [mochi.md](mochi.md) | The Mochi app. |
| [auto-improvement.md](auto-improvement.md) | Measurement-first self-improvement loop: ruler calibration, keep-or-revert cycles, draft PRs, and the integration coverage over its endpoint surface. |

## Operations

| Spec | Subsystem |
|---|---|
| [cloud.md](cloud.md) | Cloud connect and remote gateway login. |
| [connections.md](connections.md) | Third-party account connections: provider registry and tiers, the mint endpoints, grant custody at the kiro-cli boundary, warm-table prewarming, owner-only disconnect, and the L0/L1 launch gates. |
| [instances.md](instances.md) | Managing multiple instances over SSH. Sections here are cited by number from `cloud/connect.py`, so do not renumber them. |
| [dev-fleet.md](dev-fleet.md) | Worktree fleet management and pruning. |
