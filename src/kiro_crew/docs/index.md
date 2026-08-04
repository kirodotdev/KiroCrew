# Kiro Crew Documentation

Kiro Crew is a personal, autonomous AI agent that runs locally on your machine.
It's powered by kiro-cli (KiroACP) and connects to tools over the Model Context
Protocol (MCP).

## Quick Start

**Install the backend (pip) and dashboard (npm):**

```bash
pip install kirocrew
cd website && npm install && npm run build
```

Or, from a clone of the repository, run `pip install .` (or `pip install -e .`
for development) in the project root.

**Then start the gateway:**

```bash
kirocrew gateway
```

Then open `http://localhost:5476`.

See [Getting Started](getting-started.md) for the full setup, including the
agent backend and Slack credentials.

## Core Capabilities

| Capability | Description |
|------------|-------------|
| [Cron Jobs](cron-and-scheduling.md) | Schedule recurring tasks — "every weekday at 9am give me a pipeline briefing" |
| [Subagents](subagents.md) | Spawn parallel background workers for fan-out research and multi-package work |
| [Dynamic Sub-Agent Sizing](dynamic-subagent-sizing.md) | Auto-size the concurrent sub-agent cap from host memory/CPU and a learned per-agent cost |
| [Memory](memory-and-learning.md) | Persistent preferences, project context, and learned corrections across sessions |
| [Task Runner](task-runner.md) | Autonomous multi-step execution from spec files — hand it a task, walk away |
| [Research Lab](research-lab.md) | Autonomous multi-cycle research campaigns — grill-tree scoping, adaptive agent execution, exportable reports |
| [Dashboard](dashboard.md) | React web UI with multi-session chat, memory management, and live system metrics |
| [Agent Questions](agent-questions.md) | Let an agent pause mid-turn and ask you a clickable multiple-choice question |
| [Slack](slack-integration.md) | DM-based interaction with tool approval, streaming, and channel monitoring |
| [Agents](agents.md) | Switch between specialized agents per conversation, thread, or cron job |
| [Skills](skills.md) | Drop-in markdown knowledge packs for domain-specific workflows |

### Additional Features

| Feature | Description |
|---------|-------------|
| App Kit | Build and distribute apps that run inside Kiro Crew (App Store, SDK, manifest) |
| [Web Deploy](deploy-web.md) | Publish artifacts to a public HTTPS URL on your own AWS (private S3 + CloudFront + OAC) |
| Snapshot & Restore | Portable backup/restore of Kiro Crew state for machine migration |
| Eval Harness | Multi-session evaluation framework for testing agent behavior |
| Autonudge | Reactive same-session self-nudge for autonomous goal loops |
| Warm Pool | Pre-warm agent sessions for instant response |
| Cooperative Stop | Soft-stop with cancel ack before hard kill (preserves session state) |
| Streaming STT | Live speech-to-text partials in dashboard input (Whisper local; AWS Transcribe optional) |
| Memory Modes | Per-session persistent, incognito, or temporary memory |
| [Feature Tips](feature-tips.md) | Occasional personalized tips above the composer pointing at features you have not used yet |
| [Follow-up Suggestions](followup-suggestions.md) | Agent-proposed next steps above the composer — start in a new git worktree, add to this session, or skip |
| TUI | Terminal UI with Ink (React for CLI) — alternative to web dashboard |
| [Queued-Message Editing](dashboard.md) | Edit, reorder, or cancel a chat message waiting in the queue before it runs |

## Guides

- [Getting Started](getting-started.md) — Installation, first-time setup, background running
- [Configuration](configuration.md) — Config file reference, environment variables, sandbox modes
- [Use Cases](use-cases.md) — Real-world workflows from the community
- [Profiling](profiling.md) — Debug-only stack sampler (`kirocrew perf sample`) and desktop app metrics (`kirocrew desktop metrics`)
- [Dashboard iframe hosts](dashboard-iframe-hosts.md) — Which of the four embed hosts to use, why their sandboxes differ, and why an iframe can never be moved
- [Troubleshooting](troubleshooting.md) — Common issues and fixes

## Security

- OS-level sandbox with tiered modes (auto/strict/off)
- Credential redaction across all LLM output paths
- HMAC-SHA256 signed, IP-pinned dashboard tokens
- Denied-command patterns with audit logging
- Prompt-injection credential exfiltration protection
- Multi-user Slack access disabled (owner-only)
- [App Platform Trust Model](app-platform-trust-model.md) — enabled apps run in-process with full privileges; trust boundary and audit

## Contributing

- See `CONTRIBUTING.md` in the repository root for contribution guidelines
- File issues and feature requests on the project's GitHub repository
  (https://github.com/kirodotdev/KiroCrew)

## Links

- [Repository](https://github.com/kirodotdev/KiroCrew)
- [Getting Started](getting-started.md)
- [Configuration](configuration.md)
