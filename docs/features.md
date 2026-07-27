# Feature Reference

Complete list of KiroCrew capabilities. For installation and first steps, see
the [Getting Started guide](getting-started.md). For architecture diagrams, see
[Project Architecture](project-architecture.md).

> **Platforms:** macOS, Linux, and Windows (native). The OS-level sandbox is
> POSIX-only (Linux namespaces / macOS Seatbelt); all other features work on
> Windows. See [windows-install.md](windows-install.md) for per-feature status.

---

## Chat & Slack Gateway

DM KiroCrew in Slack. Each thread gets its own AI session with full tool access.

- **Socket Mode** — no public URL needed, owner-locked via `KIROCREW_OWNER_ID`
- **Dashboard-only mode** — skip Slack credentials during setup to run the web dashboard without Slack
- **Real-time streaming** — progressive message edits with typing cursor
- **Interactive tool approval** — Block Kit buttons (Approve / Reject), bidirectional sync with dashboard
- **Subagent & cron ack** — completion notifications posted to both Slack and dashboard with ack buttons
- **Context compaction** — auto-compact at configurable threshold (`session.autocompact_pct`, default 90%) via kiro-cli `/compact`; `!compact` Slack command for manual trigger
- **Message queue** — messages arriving while session is busy are queued and drained FIFO
- **Interrupt endpoint** — `POST /api/chat/slots/{slot}/interrupt` stops the current turn and processes the next message
- **Thread history** — sliding window of recent thread messages injected on every message (8k budget)
- **Thread metadata injection** — when @mentioned in channel threads, injects parent message text and reply count
- **Circuit breaker** — 5 consecutive failures → auto-reset session
- **Channel history** — group conversation context injected into LLM sessions
- **Markdown → mrkdwn** — headings, links, strikethrough, tables, mermaid diagrams converted for Slack
- **File/image/voice handling** — images sent to ACP vision, text files inline, voice memos transcribed, binary files via `file_send`
- **Slack Home Tab** — Block Kit view with live system status, cron jobs, active sessions, recent lessons
- **Per-channel activation** — `!ta` command for per-thread agent override, observe mode for group channels
- **Per-channel thread_follow** — configurable per channel; when false, requires @-mention for every message
- **A2A exchange budget reset** — agent-to-agent counts reset on human message; configurable `max_exchanges`
- **Trusted bot IDs** — multi-node mesh communication between KiroCrew instances via shared channels
- **Display names** — Slack display names used in LLM context instead of raw user IDs
- **Dashboard ↔ Slack handoff** — link dashboard sessions to Slack threads for bidirectional sync
- **Targeted send_message** — `send_message` MCP tool supports `channel`, `user`, `thread_ts`, and `reply_broadcast`
- **Inline action buttons** — Block Kit interactive elements routed back to the LLM session
- **Configurable reactions** — override phase emojis via `slack.reactions`; disable with `slack.reactions_enabled`
- **Slash command system** — `/kirocrew dashboard`, `/kirocrew @user`, `/kirocrew #channel`
- **Cooperative soft-stop** — graceful session shutdown with SIGTERM + kill fallback
- **Piper TTS** — local text-to-speech via Piper (zero cloud latency)
- **Steering file auto-load** — workspace `.kiro/steering` files automatically loaded into sessions
- **Restart from Slack** — owner-only `/kirocrew restart` slash command, gated on systemd

## Web Dashboard

Full-featured React SPA at `localhost:5476` with real-time updates via SSE.

- **Multi-slot chat** — multiple concurrent conversations with auto-generated titles
- **SSE live updates** — slot state, titles, cron/lesson/history changes pushed instantly
- **Cross-session history** — new sessions see recent exchanges from prior ones
- **CRUD panels** — manage skills, crons, lessons, and agent config from the browser
- **14-theme color system** — dark/light variants with Color Theme picker
- **Token-based auth** — optional URL token for remote access with OAuth-style refresh
- **Memory Graph Explorer** — vis.js visualization of semantic memory relationships
- **Context usage ring** — real-time token usage indicator in chat header
- **Monaco editor** — syntax-highlighted code blocks with Monaco
- **Edit & resend / Rewind** — edit past messages, replay conversation from any point
- **Fork session** — fork into a new tab with full context (or tail-only variant)
- **Regenerate replies** — regenerate with variant history navigation
- **Warm session pool** — pre-warm kiro-cli sessions for instant response
- **File picker** — `@filename` in chat triggers fuzzy file search scoped to active project
- **Project picker** — set active project directory per session
- **Session content search** — search by content with weighted ranking
- **Subagent progress bar** — compact expandable indicator showing running agents
- **Real-time tool status** — live tool call status and results streamed as they execute
- **Question cards** — `AskUserQuestion` tool calls rendered as structured option cards
- **Orchestrator mode** — Autopilot as a per-slot toggle
- **Project folder grouping** — drag-drop folders with LLM-generated emoji icons
- **Session colors** — per-session color coding with accessibility-aware contrast
- **Incognito mode** — ephemeral sessions that block memory writes
- **Streaming transcription** — live speech-to-text partials via WebSocket
- **LLM usage chart** — daily token usage tracking with model breakdown
- **Reasoning effort selector** — per-message low/medium/high effort dropdown
- **Session tags & columns** — Trello-style tag organization with drag-drop
- **Interactive widgets** — mcwidgets with bidirectional event bridge
- **Settings export/import** — one-click backup and restore of all settings
- **Tunnel integration** — reverse-tunnel support for mobile access
- **Self-update** — topbar version badge with changelog preview and one-click update

## Instances — Multi-Instance Management

Manage remote KiroCrew instances (dev hosts, EC2, home servers) from one hub
gateway over SSH tunnels. Opt-in (`kirocrew config set instances.enabled true`).

- **Auto-tunneling** — `ssh -N -L` to each remote's loopback, short-lived token mint
- **Warm set** — keeps K most-recently-used instances live; LRU eviction
- **Self-healing** — health probe + 2-tier recovery + proactive token refresh
- **Owner-only** — SEL-audited control plane, loopback-only forwards

See [instances.md](instances.md) for the full guide.

## Desktop App (Electron)

Native desktop app (macOS DMG / Linux AppImage). Bundles python-build-standalone
so end users need no Python, pip, or npm.

- **Multi-tab gateways** — connect to multiple gateways simultaneously
- **WebContentsView architecture** — modern tab/window management
- **Remote Tunnel auto-discovery** — find kirocrew binary over SSH
- **Binary resolution** — finds kirocrew on PATH or bundled backend

Build: `make desktop`. See [desktop-app.md](desktop-app.md) for details.

## Autonomous Task Runner

Execute multi-step tasks from a spec file. Designed for 10+ hour unattended operation.

```bash
kirocrew run TASK.md              # auto-resume from checkpoint
kirocrew run TASK.md --fresh      # start over
kirocrew run TASK.md --timeout 3600
```

- **Spec → Steps → Execute → Test → Retry** — LLM decomposes spec into ordered steps
- **Checkpoint resume** — crash/Ctrl+C → restart picks up from `TASK_PROGRESS.md`
- **Session recovery** — kiro-cli dies → auto-recreate and continue
- **Learn from failures** — failed steps → lessons extracted → saved for future tasks
- **Task watchdog** — alerts on 30-min stalls, enforces global timeout
- **Acceptance check** — LLM reviewer validates spec satisfaction (up to 3 rounds)
- **Plan mode** — visible, editable execution plans
- **Three access paths**: CLI, Slack (`run <path>`), Dashboard (REST API)

## Security

Defense-in-depth controls enforced at multiple layers.

- **OS-level sandbox** — Linux namespaces or macOS Seatbelt isolate kiro-cli subprocesses:

  | Mode | What's hidden | What's accessible |
  |------|---------------|-------------------|
  | `auto` (default) | `.gnupg`, `.gcloud`, `.azure`, `.docker` | `.aws`, `.ssh`, `.kube` |
  | `strict` | All credential dirs | Only `~/.ssh/known_hosts` |
  | `off` | Nothing | Everything |

- **Credential redaction** — scans all LLM output for AWS keys, private key headers, Slack tokens
- **Denied commands** — 137 regex patterns block destructive operations (cannot be bypassed in YOLO mode)
- **Tamper-resistant config** — deny patterns sourced from the bundled package, replaced on every update
- **Audit logging** — every bash command logged to `~/.kirocrew/audit.log`
- **Governance model** — two-level policy ceiling (`POLICY ∩ PROFILE`, tightest-wins) enforced at KiroCrew's own tool gate
- **Owner lock** — Slack gateway locked to `KIROCREW_OWNER_ID`; dashboard localhost-only by default
- **XSS sanitization** — DOMPurify + CSP headers

See [security-deep-dive.md](security-deep-dive.md) for the full architecture.

## Cron Scheduling

Exposed as MCP tools. Also available via CLI and Slack keywords.

```bash
kirocrew cron add "status" "report system status" --every 300
kirocrew cron list
```

- **skip_dates** — exclude specific dates (holidays)
- **timezone** — per-job timezone
- **per-job timeout_secs** — individual timeout limit
- **execution jitter** — random delay spreads load; `strict_schedule: true` to opt out
- **`--no-crons` flag** — start gateway without cron scheduler for multi-instance setups

## Proactive Push (Wait & Webhook Hooks)

- **`wait` MCP tool** — pause 60–1800s within a live session for polling workflows
- **`POST /api/hooks/agent`** — webhook endpoint for external triggers (CI alerts, email). Bearer auth, max 6 concurrent
- **`register_hook` MCP tool** — persist workflow context to `hooks.json` for cross-session continuity

## Subagent Orchestration

```bash
kirocrew spawn run "check my open CRs"     # blocking
kirocrew spawn run --async "check CRs"     # fire-and-forget
kirocrew spawn list
```

Max 3 concurrent. Configurable completion truncation, result retention TTL, and
paging over subagent output.

## Self-Learning

```bash
kirocrew learn add "always use lowercase variables" --category tool
kirocrew learn list
```

Lessons injected into every session. The task runner auto-extracts lessons from
failed steps.

## Skills System

Markdown skill files teach the LLM capabilities. Two-tier loading:

1. **Word-overlap matching** — auto-injected when message words overlap triggers
2. **Semantic fallback** — LLM reads summary, loads full content on demand

**Built-in skills**: `kirocrew-commands`, `security-assistance`, `self-nudge-loop`, `goal-loop`

Edit skills in `skills/` without rebuilding. Dashboard CRUD available at
Overview → Skills tab.

## MCP Tool Discovery

Only `kirocrew-core` and `kirocrew-cron` load at startup. Additional servers
discovered on-demand from the dashboard:

- **Discover & Sync** — scans mcp.json files, adds new servers
- **Probe All** — spawns each server, tests initialize + tools/list handshake
- **Enable/Disable** — toggle individual servers

Built-in: `kirocrew-core` (spawn, learn, task, wait, hook, send_message, file_send),
`kirocrew-cron` (cron management). Common add-ons: `slack-mcp`, `aws-outlook-mcp`.

## App Kit Platform

Build and distribute apps that run inside KiroCrew.

- **App Store** — browse, install, manage from the dashboard
- **App manifest** — declarative `app.json` with permissions, UI pages, install hooks
- **SDK** — `@kirocrew/sdk` (TypeScript), `kirocrew-client-py` (Python)
- **Gateway proxy** — `/api/apps/:id/*` proxies to app backends
- **Dependency management** — apps declare deps; platform resolves and installs
- **Gateway hooks** — lifecycle hooks (session start/end, tool call, message)
- **Chat embedding** — `ChatEmbed` SDK component for in-app chat
- **Code Review Sage** — built-in PR review app

See [app-kit/getting-started.md](app-kit/getting-started.md) for the developer guide.

## Artifacts

Persistent, versioned artifacts for widgets, code files, and documents.

- **Persistent identity** — stable slug, version history, dashboard library
- **Live-pointer model** — file-backed read/write through `source_path`
- **Explicit snapshots** — deliberate versioning (not every-edit)
- **Chat-iterate** — update via `artifact_update` MCP tool
- **Activity timeline** — lifecycle event log (FIFO 500-cap)
- **Revert** — `artifact_revert` with clean semantics
- **Auto-dedup** — atomic dedup on `source_path`

## Artifact Deploy

One-click deploy of webapp artifacts to the user's own AWS account with a public
HTTPS link, TTL, and promote-to-persistent.

- **Own-account model** — resources live in your AWS account (S3 + CloudFront)
- **TTL + reaper** — tag-gated cleanup of expired deploys
- **Live preview cards** — sandboxed gateway-rendered previews
- **IAM keystone** — KiroCrew never writes IAM; policies generated for operator

See [artifact-deploy.md](artifact-deploy.md) for the full guide.

## Snapshot & Restore

```bash
kirocrew snapshot                          # create backup
kirocrew restore ~/kirocrew-backup.tar.gz  # restore
```

Includes config, lessons, memory, cron jobs, skills, agent config, history.
Supports `--components` for selective restore and `--dry-run` preview.

## Persistent Memory

```
~/.kirocrew/workspace/memory/
├── preferences.md      # learned user preferences
├── projects.md         # active project context
└── history/YYYY-MM-DD.md  # daily conversation summaries
```

- JSONL conversation logs survive restarts
- Background LLM consolidation into structured memory
- FTS5 full-text search + vector similarity (FAISS)

See [memory-architecture.md](memory-architecture.md) for the full design.

## Eval Harness

Multi-session evaluation framework for testing agent behavior.

```bash
kirocrew eval                  # smoke test
kirocrew eval --all            # all scenarios
kirocrew gateway --test-mode   # ephemeral port + json-ready + reads approval
```

## Self-Update

```bash
kirocrew update    # git pull + rebuild + restart
```

Dashboard checks for updates on startup and every 12h. One-click update with
changelog preview.

## Runtime Stats

```bash
kirocrew status    # CLI summary
```

Message counts, tool calls, session metrics, subagent spawns. Daily health
report with thresholds (nominal / warning / critical).

---

## Further Reading

| Document | Description |
|----------|-------------|
| [getting-started.md](getting-started.md) | Installation and first steps |
| [project-architecture.md](project-architecture.md) | System architecture with diagrams |
| [security-deep-dive.md](security-deep-dive.md) | Security architecture deep dive |
| [memory-architecture.md](memory-architecture.md) | Memory system design |
| [app-kit/getting-started.md](app-kit/getting-started.md) | App Kit developer guide |
| [remote-desktop-setup.md](remote-desktop-setup.md) | 24/7 remote host setup |
| [instances.md](instances.md) | Multi-instance management |
| [install.md](install.md) | All build/install methods |
