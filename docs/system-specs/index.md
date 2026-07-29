# System Specifications

Last Updated: 2026-07-24

## How to Use

Load relevant module specs before making changes to that component. Read common patterns for cross-cutting concerns.

## Modules

| Module | Description |
|--------|-------------|
| [acp-client](modules/acp-client.md) | JSON-RPC 2.0 client for kiro-cli ACP protocol |
| [channel-history](modules/channel-history.md) | Group conversation context buffer |
| [config](modules/config.md) | Dataclass config schema and loader |
| [cli](modules/cli.md) | argparse CLI commands (chat, gateway, doctor, setup, manifest) |
| [computer-use](modules/computer-use.md) | Native desktop GUI automation: accessibility-tree snapshots, element-indexed actions, keystone primary enable, fail-closed in-gateway gate |
| [heartbeat](modules/heartbeat.md) | Periodic background tasks |
| [history](modules/history.md) | Persistent conversation history with LLM consolidation |
| [knowledge](modules/knowledge.md) | Knowledge Library ingest (FileReader/SUPPORTED formats incl. .org), folder watcher, LLMPool workers (sweep-shielded) |
| [learn-cron-dashboard](modules/learn-cron-dashboard.md) | Self-learning, cron scheduler, web dashboard |
| [mcp-apps](modules/mcp-apps.md) | SEP-1865 interactive MCP App rendering via gatewayd: marker grammar, spool schema v1 (enforced), `app-call` frame + `/api/mcp-apps/call` authorization ladder, sandboxed-iframe CSP model, single-consume slot-bound renders, 24h TTL sweep |
| [memory-skills-hooks](modules/memory-skills-hooks.md) | Memory files, skill loading, message/tool hooks |
| [messaging](modules/messaging.md) | Channel-neutral messaging transport: MessagingTransport/TurnDriver approval ladder/Renderer + ChannelLink session-key namespacing (gated via messaging.use_transport) |
| [metrics](modules/metrics.md) | OpenTelemetry-based metrics facade: namespace/attribute guardrails, redaction/privacy contract, local-first JSONL sink, default-OFF consent model (+ `KIROCREW_TELEMETRY` env opt-in), bounded local retention (age + total-size caps), opt-in OTLP egress (off by default), privacy-safe gateway boot-to-ready + bounded-cardinality per-route latency metrics |
| [persistent-agent-channels](modules/persistent-agent-channels.md) | Multi-agent collaboration channels |
| [providers](modules/providers.md) | LLM provider abstraction (KiroACP / kiro-cli — the sole provider) |
| [security](modules/security.md) | Defense-in-depth: sandbox, XPIA hardening, auth, denied commands |
| [sel](modules/sel.md) | Security Event Log — immutable audit trail for tool invocations |
| [session](modules/session.md) | Thread-keyed ACP session pool with idle expiry |
| [subagent](modules/subagent.md) | Background agent spawning with reaper, approval cascade, turn limits |
| [slack-gateway](modules/slack-gateway.md) | Slack Socket Mode gateway, handler, client abstraction |
| [task](modules/task.md) | Task state machine |
| [taskrunner](modules/taskrunner.md) | Autonomous multi-step task executor with git coordination |

## Common Patterns

| Pattern | Description |
|---------|-------------|
| [error-handling](common/error-handling.md) | Exception hierarchy and error boundaries |
| [testing-conventions](common/testing-conventions.md) | pytest patterns, mocking, test structure |

## Design

| Document | Description |
|----------|-------------|
| [architecture-overview](design/architecture-overview.md) | High-level system architecture and ACP protocol |

## Features

| Feature | Description |
|---------|-------------|
| [app-notifications](features/app-notifications.md) | App notification producers: push endpoint, manifest channels, rate limiting |
| [claude-code-provider](features/claude-code-provider.md) | Removed — KiroCrew is KiroACP/kiro-cli only; documents the dormant ACP seam that remains |
| [code-approvers](features/code-approvers.md) | Tier-based CR reviewer routing with drift validator |
| [dashboard-token-auth](features/dashboard-token-auth.md) | Slack-gated HMAC token authentication for dashboard |
| [inline-action-buttons](features/inline-action-buttons.md) | Interactive buttons in chat messages |
| [prompt-optimizer](features/prompt-optimizer.md) | Native pre-send prompt optimization (Cmd+Shift+Enter) |
| [stt-streaming](features/stt-streaming.md) | Live speech-to-text via AWS Transcribe Streaming |
| [steering-viewer](features/steering-viewer.md) | Steering tab under Agent Capabilities: view + edit `~/.kiro/steering` and project `.kiro/steering` files |
| [turn-complete-chime](features/turn-complete-chime.md) | Notification sound whenever the agent finishes a turn in any chat |
| [turn-stats-footer](features/turn-stats-footer.md) | Per-turn elapsed time + credits footer under assistant messages (kiro-cli parity) |
| [voice-streaming](features/voice-streaming.md) | Real-time Polly TTS with streaming auto-speak and interrupt |
| [workflow-chat-cards](features/workflow-chat-cards.md) | Inline launch + completion cards for workflows in chat, clickable to the Workflows panel |
