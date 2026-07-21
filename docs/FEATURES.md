# KiroCrew

> ⚠️ **Be mindful of what you share with any AI agent. Avoid pasting secrets, credentials, or sensitive personal data into KiroCrew chats.**

Open-source personal AI agent (CLI + Slack gateway + web dashboard). Powered by the `kiro-cli` agent over ACP (Agent Client Protocol) — the only provider. **[What's New](../CHANGELOG.md)**

```
CLI / Slack DM / Dashboard → KiroCrew → ACP agent backend → LLM + MCP tools
```

## Quick Start

KiroCrew installs as a Python backend (`pip`) plus a React dashboard (`npm`).
See the [README](../README.md#quick-start) for the full walkthrough.

```bash
git clone https://github.com/kirodotdev/KiroCrew.git
cd kirocrew

# Build the dashboard, then install the backend (bundles it)
cd website && npm install && npm run build && cd ..
cp -r website/dist src/kiro_crew/static/dist
pip install .

kirocrew setup                # interactive wizard
kirocrew gateway              # open http://localhost:5476
```

> **Windows is supported natively** (via `kiro_crew.platform_compat`), alongside macOS and Linux. Note the **OS-level sandbox** is POSIX-only — it relies on Linux namespaces or macOS Seatbelt — so on Windows that isolation layer is unavailable; the core gateway, chat, cron, and dashboard all work. See [WINDOWS_INSTALL.md](WINDOWS_INSTALL.md) for per-feature status.
>
> **Vector memory** embeddings run in-process via a bundled llama-cpp-python runtime — nothing to install. The embedding model (~610MB) downloads automatically in the background on first gateway start; memory uses keyword search until it lands.
>
> **Node.js**: Standardized on Node 16 via nvm for GLIBC compatibility across all platforms. Vite 5 supports Node 16+.

See [DEPENDENCIES.md](../DEPENDENCIES.md) for the full dependency list and manual install instructions.

### Auto-Start Gateway (macOS Launch Agent)

Run the gateway as a launchd service so it starts on login and auto-restarts on crash:

```bash
# Set your actual paths
KIROCREW_BIN=$(which kirocrew)                    # or specify full path
KIROCREW_DIR="$HOME/kirocrew"                     # update this to your clone
mkdir -p ~/Library/Logs/KiroCrew

cat > ~/Library/LaunchAgents/dev.kirocrew.gateway.plist << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>dev.kirocrew.gateway</string>
    <key>ProgramArguments</key>
    <array>
        <string>$KIROCREW_BIN</string>
        <string>gateway</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$KIROCREW_DIR</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>KIROCREW_PROJECT_DIR</key>
        <string>$KIROCREW_DIR</string>
        <key>PATH</key>
        <string>$KIROCREW_DIR/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$HOME/Library/Logs/KiroCrew/gateway.log</string>
    <key>StandardErrorPath</key>
    <string>$HOME/Library/Logs/KiroCrew/gateway.err</string>
</dict>
</plist>
EOF
```

Paths are expanded at creation time via shell variables. Then load:

```bash
launchctl load ~/Library/LaunchAgents/dev.kirocrew.gateway.plist
```

Manage with: `launchctl start|stop dev.kirocrew.gateway`. Uninstall with `launchctl unload` + `rm` the plist. (Or just use `kirocrew service install` / `uninstall`, which manages this plist for you.)

### Remote Host (24/7 Operation)

Run KiroCrew on an always-on remote Linux host (a VPS or cloud VM) so the Slack bot, cron jobs, and task runner work while your laptop sleeps.

**Recommended setup:**
- **OS**: A current Linux distribution (e.g. Ubuntu 22.04+ or a recent Debian/Fedora release)
- **Resources**: ~16 vCPU and 64GB RAM is comfortable for heavy use. KiroCrew itself uses ~10GB RAM, but MCP cold starts and CPU-intensive tool calls can cause memory spikes well beyond that. Extra vCPUs help with CPU-intensive tool calls and parallel subagent execution.
- **Architecture**: x86_64 or arm64

See [REMOTE_DESKTOP_SETUP.md](REMOTE_DESKTOP_SETUP.md) for the full setup guide.

## Features

### Chat & Slack Gateway

DM KiroCrew in Slack. Each thread gets its own AI session with full tool access.

- **Socket Mode** — no public URL needed, owner-locked via `KIROCREW_OWNER_ID`
- **Dashboard-only mode** — skip Slack credentials during setup to run the web dashboard without Slack
- **Real-time streaming** — progressive message edits with ▍ cursor
- **Interactive tool approval** — Block Kit buttons (✅ Approve / 🚫 Reject), bidirectional sync with dashboard
- **Subagent & cron ack** — completion notifications posted to both Slack and dashboard with ack buttons
- **Context compaction** — auto-compact at configurable threshold (`session.autocompact_pct`, default 90%) via kiro-cli `/compact`; `!compact` Slack command for manual trigger
- **Message queue** — messages arriving while session is busy are queued with ⏳ reaction and drained FIFO
- **Interrupt endpoint** — `POST /api/chat/slots/{slot}/interrupt` stops the current LLM turn and immediately processes the next queued message ("Send Now" functionality)
- **Thread history** — sliding window of recent thread messages injected on every message (8k budget, newest first)
- **Thread metadata injection** — when @mentioned in channel threads, injects parent message text and reply count via conversations.replies fallback
- **Circuit breaker** — 5 consecutive failures → auto-reset session
- **Channel history** — group conversation context injected into LLM sessions
- **Markdown → mrkdwn** — headings, links, strikethrough, tables, mermaid diagrams converted for Slack
- **File/image/voice handling** — images sent to ACP vision, text files injected inline, voice memos transcribed via whisper, binary files (audio/video) supported via `file_send` + outbox with proper Content-Type
- **Slack Home Tab** — Block Kit view with live system status, cron jobs, active sessions, recent lessons
- **Per-channel activation** — `!ta` command for per-thread agent override, observe mode for group channels
- **Per-channel thread_follow** — configurable `thread_follow` boolean per channel; when false, requires @-mention for every message even in active threads
- **A2A exchange budget reset** — agent-to-agent exchange counts reset on human message; configurable `max_exchanges` per channel
- **Trusted bot IDs** — multi-node mesh communication between KiroCrew instances via shared channels
- **Display names** — Slack display names used in LLM context instead of raw user IDs
- **Thinking content filter** — extended thinking blocks stripped from Slack messages
- **Dashboard ↔ Slack handoff** — link dashboard sessions to Slack threads for bidirectional sync; `sessions` command lists recent sessions with resume buttons
- **Targeted send_message** — `send_message` MCP tool supports `channel`, `user`, `thread_ts`, and `reply_broadcast` params
- **Inline action buttons** — Block Kit interactive elements routed back to the LLM session
- **Configurable reactions** — override phase reaction emojis via `slack.reactions` config; disable entirely with `slack.reactions_enabled`
- **Open channels** — `slack.open_channels` config bypasses allowlist for specified channels
- **Slash command system** — `/kirocrew dashboard`, `/kirocrew @user`, `/kirocrew #channel` with configurable command name
- **Granular unfurl control** — `unfurl_links` and `unfurl_media` params on `send_message`
- **Cooperative soft-stop** — graceful session shutdown with SIGTERM + kill fallback
- **Piper TTS** — local text-to-speech via Piper as alternative to AWS Polly (zero cloud latency)
- **Steering file auto-load** — workspace `.kiro/steering` files automatically loaded into kiro-cli sessions
- **Enterprise Grid null-team handling** — graceful handling of null team field in Grid interaction payloads
- **Bang command validation** — unrecognized `!` commands caught instead of falling through to LLM
- **Restart from Slack** — owner-only `/kirocrew restart` slash command and `!restart` bang, gated on systemd (`INVOCATION_ID`); performs a clean `os._exit`-based supervisor respawn

### Web Dashboard

Full-featured React SPA at `localhost:5476` (or `http://kirocrew.localhost:5476`) with real-time updates.

- **React + TypeScript + Tailwind** — Vite 5-built SPA with Redux Toolkit state management and React Router
- **Multi-slot chat** — multiple concurrent conversations with pagination and auto-generated session titles
- **SSE live updates** — server-sent events push slot state, titles, cron/lesson/history changes instantly (no polling)
- **Runtime stats** — message counts, tool calls, session metrics, health status
- **Cross-session history** — new dashboard sessions see recent exchanges from prior ones
- **Notification center** — dedicated notifications page with categories, search, and date grouping
- **CRUD panels** — manage skills, crons, lessons, and agent config from the browser
- **Collapsible sidebar** — full (220px) or icon-only (56px) navigation, state persisted in localStorage
- **XSS protection** — all rendered HTML sanitized via DOMPurify
- **Self-update** — topbar shows version badge; click to check for updates with changelog preview, "Update Now" button to pull + rebuild + auto-restart
- **Custom domain** — `kirocrew setup` optionally adds `kirocrew.localhost` to the hosts file (macOS/Linux)
- **Branding** — custom bot name and avatar via `dashboard.bot_name` / `dashboard.avatar` config
- **14-theme color system** — dark/light variants with Color Theme picker in Overview > Display tab; theme mode/color and onboarding are workspace-persistent server-side (persist across ports/devices) and served pre-auth via `GET /api/theme/boot`
- **Session restore** — optionally restore active sessions on gateway restart
- **Token-based auth** — optional dashboard URL token for remote access; token sessions auto-renew via OAuth-style refresh tokens (`POST /api/auth/refresh`, `GET /api/auth/me`, 30-day max TTL) so long-lived dashboards stay signed in without re-pasting the URL token
- **Memory Graph Explorer** — vis.js visualization of semantic memory relationships
- **Context usage ring** — real-time token usage indicator in chat header
- **Configurable display** — zoom level, font family, and theme in Overview > Display tab
- **Monaco editor** — syntax-highlighted code blocks in chat with Monaco editor
- **JSON syntax tokens** — colored JSON keys/strings/numbers/booleans across all themes
- **Prompts & Agent-SOP** — manage prompt templates and Agent SOPs from Overview > Prompts tab
- **Settings page** — dedicated settings page with General, Chat, and Display panels
- **Developer page** — log viewer with Virtuoso-based virtual scrolling
- **Capabilities page** — overview of installed MCP tools and agent capabilities
- **Schedule page** — week grid view of cron jobs with job detail panel
- **Colored diff rendering** — tool approval popups and activity viewer render diffs with colored +/- lines
- **Inline image preview** — drag-drop images show preview strip before sending
- **Persistent agent channels** — multi-agent collaboration UI with dedicated channel pages
- **Orchestrator mode** — Autopilot is a per-slot mode of Chat (toggle via `PATCH /api/chat/slots/{slot}/mode`, blocked while the slot is running); the standalone orchestrated app was removed
- **Project folder grouping** — organize sessions into folders with drag-drop, LLM-generated emoji icons, server-persisted pinning
- **Session colors** — per-session color coding with 4 palette generators and accessibility-aware contrast
- **Incognito mode** — ephemeral sessions that block `learn_add` and memory consolidation
- **File picker** — `@filename` in chat input triggers fuzzy file search scoped to active project
- **Project picker** — set active project directory per session; recent projects + directory browser
- **Session archive viewer** — browse rotated/compacted session archives under Developer page
- **Subagent progress bar** — compact expandable indicator showing running agents and current tools
- **Real-time tool status** — live tool call status and results streamed to the dashboard as they execute
- **Question cards** — `AskUserQuestion` tool calls intercepted and broadcast as structured `question_card` events with options UI
- **Session content search** — search history by content (CR IDs, error messages, file paths)
- **Collapsible tool calls** — completed agent turns collapse tool calls by default
- **`kirocrew stop` command** — stop a running gateway via SIGTERM with port-based PID lookup
- **`kirocrew service` command** — install/uninstall/status for system-level daemon (systemd on Linux, launchd on macOS) with auto-restart on crash
- **`kirocrew logs` command** — tail systemd journal, launchd stdout, or gateway.log depending on install type
- **CSRF protection** — allowed origin derived from `dashboard.url` config
- **Edit & resend** — edit and resend previous user messages with history preserved
- **Rewind** — edit any past user message in place and replay the conversation from that point
- **Fork session** — fork a session into a new tab with full context carried over
- **Tail-only fork** — opt-in fork variant (Settings › Chat) that keeps only the messages after the chosen point, verbatim; the earlier messages are dropped. Controlled by a single Settings toggle — the fork icon itself no longer changes appearance.
- **Regenerate replies** — regenerate assistant replies with variant history navigation
- **Warm session pool** — pre-warm kiro-cli sessions with configurable default agent for instant response
- **Tool purpose pills** — tool call labels show purpose text, persisted across reloads
- **Batch tool rejection** — reject multiple pending tool approvals at once
- **Persistent tool tracking** — tool approval state persists across page reloads
- **Streaming transcription** — live speech-to-text partials in dashboard input via WebSocket
- **Cancel queued messages** — cancel button for messages waiting in the queue
- **Weighted content search** — session content search with weighted ranking for better relevance
- **Kiro Usage analytics** — session analytics with token usage, tool call counts, and trends
- **Prompt history** — ↑/↓ arrow keys navigate through previous prompts in chat input
- **iOS-style queue stack** — queued messages displayed as a visual stack
- **Panda Den scene** — bamboo forest office with panda avatars
- **Worlds popout** — pop out scene views into separate windows
- **SSE install logs** — streaming install progress for App Store apps
- **Autonudge service** — reactive same-session self-nudge for autonomous loops
- **Memory mode** — persistent, incognito, or temporary memory per session
- **Review activation mode** — channel responses require explicit activation before replying
- **Merge queued messages** — optionally merge queued messages into a single prompt
- **Hooks page** — display kiro-cli agent hooks in a dedicated dashboard page
- **Interactive widgets** — mcwidgets support bidirectional `data-action` event bridge for in-chat forms and controls
- **Settings export/import** — one-click backup and restore of all settings, lessons, and memory
- **LLM usage chart** — daily token usage tracking with provider/model breakdown and filters
- **Reasoning effort selector** — per-message reasoning effort dropdown for effort-capable kiro-cli models (low/medium/high)
- **Shareable session URLs** — deep-link to specific messages within sessions via URL parameters
- **Navigation panel** — context-aware link panel with extracted URLs and smart labels
- **Session tags & columns** — Trello-style tag-based session organization with drag-drop columns
- **Sidebar pin/close/unread** — ⋮ context menu with pin, close, and toggle read/unread markers
- **Drag-reorder apps** — reorder Apps section in sidebar via drag-and-drop
- **Mobile swipe gesture** — swipe from edge to open/close chat sidebar on mobile
- **Fix with AI** — failed app installs show "Fix with AI" button for agent diagnosis
- **Chat embedding** — App SDK `ChatEmbed` component for embedding chat in app UIs
- **Resizable thread sidebar** — drag to resize thread reply panel in channel view
- **Auto-compaction notice** — visual indicator when context auto-compaction triggers
- **FollowUp bar toggle** — click follow-up options to toggle text in input field
- **Tool approval visibility** — improved visual prominence of tool approval boxes
- **Strict schedule toggle** — cron creation UI includes strict_schedule checkbox
- **Tunnel integration** — optional reverse-tunnel support for mobile dashboard access; dynamic CORS, presigned links use the tunnel URL, status pill in top bar
- **Tool I/O detail panel** — tool input/output persisted on message meta for inline inspection
- **Steering resource loading** — workspace `.kiro/steering` resources loaded into dashboard sessions

### Instances — Multi-Instance Management

Manage and switch between several **remote** KiroCrew instances (dev hosts, EC2, home servers) from one hub gateway over SSH tunnels, embedding each remote dashboard in a single `/instances` page. Opt-in (`kirocrew config set instances.enabled true`).

- **Auto-tunneling** — opens `ssh -N -L` to each remote's loopback dashboard and mints a short-lived token on connect
- **Warm set** — keeps the K most-recently-used instances live (hide-not-unmount); evicts LRU beyond `instances.warm_set_cap`
- **Self-healing** — health probe + 2-tier recovery + proactive token refresh; on-demand Diagnose and remote Restart
- **Owner-only** — SEL-audited control plane, never reachable via Slack; loopback-only forwards, tokens never logged
- See [docs/INSTANCES.md](INSTANCES.md) for the full guide

### Desktop App (Electron)

Native desktop app (macOS DMG / Linux AppImage) wrapping the web dashboard. Bundles a python-build-standalone interpreter so end users need no Python, pip, or npm; auto-starts the gateway and connects to `localhost:5476`.

- **Multi-tab gateways** — connect to multiple KiroCrew gateways simultaneously in separate tabs
- **WebContentsView architecture** — modern tab/window management replacing legacy BrowserView
- **Remote Tunnel auto-discovery** — auto-discover kirocrew binary over SSH in Remote Tunnel mode
- **Binary resolution** — finds the kirocrew binary on `PATH` (or the bundled backend) for Electron launches

The Electron wrapper lives in `website/electron/`. Build a distributable app with:

```bash
make desktop
# → website/electron/dist/KiroCrew-*.dmg (macOS) or *.AppImage (Linux)
```

For local development you can run the wrapper directly:

```bash
cd website/electron && npm install && npx electron .
```

See `website/electron/README.md` for build and packaging details.

### Autonomous Task Runner

Execute multi-step tasks from a spec file. Designed for 10+ hour unattended operation.

```bash
kirocrew run TASK.md              # auto-resume from checkpoint
kirocrew run TASK.md --fresh      # ignore checkpoint, start over
kirocrew run TASK.md --timeout 3600  # global timeout (seconds)
kirocrew run TASK.md --no-test    # skip test verification
```

- **Spec → Steps → Execute → Test → Retry** — LLM decomposes spec into ordered steps
- **Checkpoint resume** — crash/Ctrl+C → restart picks up from TASK_PROGRESS.md
- **Session recovery** — kiro-cli process dies → auto-recreate and continue (separate budget: 2 crash recoveries vs 3 logic retries)
- **Learn from failures** — failed steps → LLM extracts lessons → saved for future tasks
- **History integration** — steps logged to ConversationLog → consolidated into memory
- **Task watchdog** — alerts on 30-min stalls, enforces global timeout
- **Acceptance check** — after all steps pass, LLM reviewer validates spec satisfaction; generates visible remediation steps if needed (up to 3 rounds)
- **Plan mode** — visible, editable execution plans with expandable step descriptions; acceptance check appears as final step in all modes
- **Three access paths**: CLI (`kirocrew run`), Slack (`run <path>`), Dashboard (REST API)

### Security

Defense-in-depth security controls enforced at multiple layers.

- **OS-level sandbox** — isolates kiro-cli subprocesses using Linux user/mount namespaces or macOS Seatbelt. Three modes configurable via `agent.sandbox` in `~/.kirocrew/config.json`:

  | Mode | Config | What's hidden | What's accessible |
  |------|--------|---------------|-------------------|
  | **Standard** | `"auto"` (default) | `.gnupg`, `.gpg`, `.gcloud`, `.azure`, `.docker` | `.aws`, `.ssh`, `.kube` |
  | **Strict** | `"strict"` | All credential dirs including `.aws`, `.ssh`, `.kube` | Only `~/.ssh/known_hosts` |
  | **Off** | `"off"` | Nothing | Everything |

  Standard mode enables git-over-SSH, AWS CLI (via `credential_process`), and kubectl while keeping non-workflow credential stores hidden. Accessible directories are still protected: the hook layer blocks direct file reads (`cat ~/.aws/credentials`), denied commands block env var dumping and script-based extraction, and `redact_credentials()` scrubs any credential patterns from LLM output. Env vars (`AWS_SECRET*`, `SSH_AUTH_SOCK`) are scrubbed in all modes.

  ```json
  {"agent": {"sandbox": "strict"}}
  ```

- **Credential output redaction** — `redact_credentials()` scans all LLM output for credential patterns (AWS access keys, secret keys, session tokens, private key headers, Slack tokens) and base64-encoded variants before posting to Slack or dashboard
- **deniedCommands** — 116 regex patterns block destructive operations at the kiro-cli level (AWS delete/terminate, git push, rm -rf, SQL drops, IaC destroy, S3 upload exfiltration, env var dumping, IMDS access, script-based credential extraction). Cannot be bypassed by the LLM even in YOLO mode
- **Tamper-resistant config** — deniedCommands always sourced from the bundled package and replaced (not merged) on every update; stale patterns from old versions are automatically cleaned up
- **Audit logging** — every bash command execution logged to `~/.kirocrew/audit.log` with UTC timestamps via kiro-cli preToolUse hook
- **Built-in tool deny list** — `security.py` blocks tool names matching credential/destructive patterns at the Python hook layer
- **System prompt rules** — explicit negative instructions (no git push, no credential file reads, no destructive commands)
- **Owner lock** — Slack gateway locked to `KIROCREW_OWNER_ID`; dashboard bound to localhost only
- **Localhost-only default** — when `dashboard.url` is empty, the dashboard binds to `127.0.0.1` regardless of Slack configuration. To expose the dashboard on the network, explicitly set `dashboard.url` to your hostname in `~/.kirocrew/config.json`. When network-exposed, token authentication is enforced — access the dashboard via SSH tunnel (`ssh -L 5476:localhost:5476 your-host`) for defense-in-depth
- **CLI commands** — `kirocrew security deny-list` shows active patterns; `kirocrew security audit` scans history for suspicious activity
- **Auto-propagation** — deny list updates ship with package updates; `kirocrew update` automatically refreshes agent config
- **XSS sanitization** — rehypeSanitize in markdown renderer + CSP headers prevent stored XSS via chat content
- **Git push scoped exception** — unified deny patterns with scoped exception for `git stash` operations
- **Kill-kirocrew regex anchoring** — anchored regex prevents partial-match exploitation
- **Cross-fs sandbox fix** — tmpfs bind source works across filesystem boundaries with credential env propagation
- **Process leak mitigation** — reduced per-session MCP footprint, silenced 404 retry storm, escaped child process tracking

### Runtime Stats

Track operational metrics across all components. Available via CLI, Slack, and dashboard.

```bash
kirocrew status               # CLI stats summary
```

Slack: `status` keyword. Dashboard: System page.

- Message counts, tool calls, session starts, subagent spawns, timeouts
- Daily report with health thresholds (nominal / warning / critical)

### Cron Scheduling

Exposed as MCP tools — kiro-cli calls them directly. Also available via CLI and Slack keywords.

```bash
kirocrew cron add "status" "report system status" --every 300
kirocrew cron list
```

Slack: `cron list`, `cron remove <id>`, `cron pause <id>`, `cron resume <id>`

- **skip_dates** — exclude specific dates from recurring jobs (e.g. holidays)
- **timezone** — per-job timezone for skip_dates evaluation
- **`--no-crons` flag** — start gateway without cron scheduler for multi-instance setups
- **per-job timeout_secs** — individual job timeout limit; jobs exceeding it are killed and reported
- **execution jitter** — random delay (0-20min hourly, 0-2h daily) spreads load; `strict_schedule: true` to opt out
- **configurable subagent timeout** — `subagent.timeout_secs` in config.json overrides default 5-minute limit

### Proactive Push (Wait & Webhook Hooks)

Two mechanisms for autonomous workflows that wait for external systems:

- **`wait` MCP tool** — pause 60–1800s within a live session. Use for interactive loops: submit CR → wait → check for AutoSDE comments → fix → repeat.
- **`POST /api/hooks/agent`** — OpenClaw-style webhook endpoint for external triggers (CI alerts, email notifications, automation platforms). Ephemeral sessions with context from `~/.kirocrew/hooks.json`. Bearer token auth, max 6 concurrent.
- **`register_hook` MCP tool** — persist workflow context to `hooks.json` for cross-session continuity.

### Subagent Orchestration

```bash
kirocrew spawn run "check my open CRs"     # blocking
kirocrew spawn run --async "check CRs"     # fire-and-forget
```

Slack: `spawn <task>`, `bg <task>`, `spawn list`. Max 3 concurrent.

- **Configurable completion truncation** — `agent.completion_keep` (`head`/`tail`/`both`) and `agent.completion_keep_chars` control which end of the subagent transcript survives in the completion event
- **Completion summary + result_path** — truncated/orchestrator results deliver a first+last-words preview plus a `result_path` instead of a lossy blob; the parent reads the full transcript on demand
- **Result retention TTL** — `agent.subagent_result_ttl_secs` (default 3600s) reaps delivered-result tombstones; other tombstones keep 7 days
- **`spawn_status` paging** — `offset`/`limit`/`grep` params for line-oriented paging and case-insensitive regex over subagent output
- **PostToolUse hook** — subagent loop now fires `PostToolUse` on `EVENT_TOOL_RESULT` (mirrors chat_runner behavior for full hook observability)
- **Hook payload metadata** — `subagent_id`, `parent_session_key`, and `agent_role` passed into hook payloads for per-subagent attribution

### Self-Learning

```bash
kirocrew learn add "always use lowercase variables in functions" --category tool
kirocrew learn list
```

Lessons are injected into every session context automatically. The task runner also auto-extracts lessons from failed steps.

### Skills System

Markdown skill files teach the LLM which CLI commands exist. Two-tier loading:
1. **Word-overlap matching** — skill content auto-injected when message words overlap with skill triggers; `!`-prefixed triggers exclude matches
2. **Semantic fallback** — LLM reads skill summary, `cat`s the file on demand

**Lazy skill injection** (`skills.lazy_load`, default off) — when on, injects `always: true` pinned skills plus a usage-ranked top-K of on-demand skills under per-section context budgets; the long tail is discoverable via the `skill_search` MCP tool.

Edit skills in `skills/` without rebuilding. See `skills/README.md`.

**Dashboard CRUD**: create, edit, and delete skills from `localhost:5476` → Overview → Skills tab.

**Built-in skills** (ship with the package):
- `kirocrew-commands` — Slack commands, dashboard access, setup reference
- `security-assistance` — ARCC governance search for security-sensitive requests
- `self-nudge-loop` — scaffold autonomous same-session loops with AutoNudge
- `goal-loop` — goal-driven self-improving loop on top of self-nudge-loop

### MCP Tool Discovery

Only `kirocrew-core` and `kirocrew-cron` are loaded at startup — no auto-scan. Additional MCP servers (including `slack-mcp`) are discovered on-demand from the dashboard:
- **⚡ Discover & Sync** — scans `~/.kiro/settings/mcp.json` and `~/.kirocrew/mcp.json`, adds new servers to `agents/defaults.json`
- **🔍 Probe All** — spawns each MCP server, sends initialize + tools/list handshake
- **Enable/Disable** — toggle individual servers without removing them
- **Live badges** — color-coded server status (Online/Error/Unknown)

Install additional MCP servers by adding them to `~/.kirocrew/mcp.json` (or `~/.kiro/settings/mcp.json`) and clicking **Discover & Sync** in the dashboard.

Built-in MCP servers:
- `kirocrew-core` — spawn, learn, task, wait, hook, send_message, file_send tools (native MCP, auto-configured)
- `kirocrew-cron` — cron job management (native MCP, auto-configured)

Common on-demand servers:
- `slack-mcp` — Slack integration (discovered when Slack is configured)
- `aws-outlook-mcp` — email and calendar via Outlook (optional)

### App Kit Platform

Build and distribute apps that run inside KiroCrew. Apps can be dashboard-hosted (iframe/SDK), gateway-side (Python backend), or external (Electron, CLI).

- **App Store** — browse, install, and manage apps from the dashboard with SSE streaming install logs
- **App manifest** — declarative `app.json` with metadata, permissions, UI pages, and install hooks
- **SDK ecosystem** — `@kirocrew/sdk` for TypeScript apps, `kirocrew-client-py` for Python apps
- **Federated loading** — apps loaded via import maps with AppHost isolation
- **Gateway proxy** — `/api/apps/:id/*` proxies requests to app backends
- **Dependency management** — apps declare dependencies; the platform resolves and installs them
- **Gateway hooks** — apps register lifecycle hooks (session start/end, tool call, message) for analytics and guardrails
- **Chat embedding** — `ChatEmbed` SDK component embeds a full chat interface within app UIs
- **Fix with AI** — failed installs show "Fix with AI" button that sends errors to the agent
- **Builtin auto-discovery** — frontend automatically discovers and registers routes for builtin apps
- **App enable/disable metrics** — track app adoption with enable/disable event metrics
- **Code Review Sage (built-in)** — reviews GitHub PRs; posts a PENDING (human-submitted) review and requires an authenticated `gh` on the gateway host

See [app-kit/getting-started.md](app-kit/getting-started.md) for the full developer guide.

### Artifacts

Persistent, versioned artifacts for chat-rendered widgets, code files, and documents. Unified file viewer and artifact surfaces share a single mental model.

- **Persistent identity** — stable slug, version history, and dashboard library (artifacts survive chat scrollback)
- **Live-pointer model** — file-backed artifacts read/write through `source_path` on disk; edits in file viewer or artifacts write to the same path
- **Explicit snapshots** — versioning is deliberate (Snapshot button) rather than every-edit; `live_dirty` flag drives the Snapshot button when live content drifts from latest snapshot
- **Chat-iterate** — update artifacts via chat with `artifact_update` MCP tool; iterate on content without leaving the conversation
- **Activity timeline** — lifecycle event log (created/edited/iterated/reverted/referenced) with FIFO 500-cap per artifact; `GET /api/artifacts/{slug}/events`
- **Comments-to-chat** — file viewer comments surface in the chat session for agent awareness
- **Revert** — `artifact_revert` MCP tool with clean revert semantics (reads target version, writes as new live with `reverted` event)
- **Auto-dedup** — atomic dedup on `source_path` at API layer (200=bumped existing vs 201=created new)
- **CLI** — `kirocrew artifact list/show/save/update/delete/versions`

### Artifact Deploy

One-click deploy of webapp artifacts into the user's own AWS account with a global public HTTPS link (Vercel-like), default TTL, and promote-to-persistent.

- **Own-account model** — KiroCrew orchestrates via a named AWS profile; resources (S3 + CloudFront, optional Lambda/DynamoDB) live in the user's account
- **TTL + reaper** — finite-TTL deploys require the in-account reaper stack; tag-gated cleanup that can only touch `kirocrew:managed` resources
- **Live preview cards** — browser-framed artifact cards render the app's local copy through a sandboxed, token-gated gateway channel; deployed sites embed the remote page when provably framable
- **Deploy console** — sidebar surface for AWS profiles (register/verify), fleet view, cost estimates (labelled *not a bill*), and teardown
- **IAM keystone** — KiroCrew never writes IAM; policies are generated for the operator to apply

See [artifact-deploy.md](artifact-deploy.md) for the full guide.

### Snapshot & Restore

Portable backup and restore of KiroCrew state for migration between machines.

```bash
kirocrew snapshot                          # create snapshot
kirocrew snapshot --list                   # list existing snapshots
kirocrew restore ~/kirocrew-backup.tar.gz  # restore from snapshot
```

Includes: config, lessons, memory, cron jobs, skills, agent config, conversation history. Supports selective restore via `--components` and `--dry-run` preview.

### Eval Harness

Multi-session evaluation framework for testing agent behavior with full memory loop.

```bash
kirocrew eval                              # run smoke test
kirocrew eval memory_recall_basic          # run specific scenario
kirocrew eval --all                        # run all scenarios
```

Composable gateway flags for test harnesses:

```bash
kirocrew gateway --test-mode               # ephemeral port + json-ready + reads approval
kirocrew gateway --port auto --json-ready  # OS-assigned port, prints KIROCREW_READY JSON
kirocrew gateway --approval yolo           # auto-approve all tools (requires KIROCREW_HOME override)
kirocrew gateway --approval reads          # auto-approve read-only tools
```

See [src/kiro_crew/eval/README.md](../src/kiro_crew/eval/README.md) for scenario format and usage.

### Persistent Memory

```
~/.kirocrew/workspace/memory/
├── preferences.md      # learned user preferences
├── projects.md         # active project context
└── history/2026-02-18.md  # daily conversation summaries
```

- Conversations persist as JSONL — survive restarts
- Background LLM consolidation into structured memory
- FTS5 full-text search across all memory files

### Self-Update

```bash
kirocrew update               # git pull + rebuild
```

The dashboard also checks for updates on startup and every 12 hours. When a newer version is available on the remote branch, the topbar version badge turns into "📦 Update Available". Clicking it shows the changelog diff; click "Update Now" to pull, rebuild, and auto-restart the gateway.

## Setup

1. Create a Slack App — see [SLACK_SETUP.md](../SLACK_SETUP.md) for the full walkthrough (manifest import, workspace approval, token generation)
2. Run `kirocrew setup` and paste your tokens

**Dashboard-only mode**: Leave both Slack tokens empty during `kirocrew setup` to skip Slack and run the web dashboard only via `kirocrew gateway`.

## Architecture

The frontend (React SPA) lives in `website/`; the Python backend lives under `src/kiro_crew/`. Production frontend builds are bundled into `src/kiro_crew/static/dist/`.

```
src/kiro_crew/
├── cli.py               # argparse CLI (chat, gateway, run, cron, spawn, learn, config, doctor, setup, status, update)
├── taskrunner.py        # autonomous task executor (orchestrator)
├── task_executor.py     # task step execution engine
├── task_models.py       # task data models
├── task_planner.py      # task planning engine
├── task_reporter.py     # task reporting
├── session.py           # thread → LLM session pool with compaction + warm pool
├── context.py           # memory + skills + hooks + lessons → prompt context
├── context_management.py # conductor context isolation for multi-agent sessions
├── history.py           # JSONL conversation log + LLM consolidation + title persistence
├── memory.py            # structured memory files + FTS5 search
├── vector_memory.py     # vector-based semantic memory with FAISS
├── learn.py             # lesson store (JSONL)
├── skills.py            # skill markdown loader (fuzzy word-overlap matching)
├── hooks.py             # config-driven message/tool hooks
├── heartbeat.py         # periodic background tasks
├── subagent.py          # background agent orchestration
├── channel.py           # persistent agent channels
├── conductor_skill.py   # agent delegation conductor
├── sync_bridge.py       # sync-to-async bridge for MCP tools
├── agent_metadata.py    # agent metadata extraction
├── session_workspace.py # session workspace management
├── autonudge.py         # reactive same-session self-nudge service
├── snapshot.py          # portable snapshot and restore for KiroCrew state
├── cron.py              # scheduled job service (silent mode, per-cron approval)
├── transcribe.py        # voice memo STT (whisper or AWS Transcribe Streaming)
├── voice_reply.py       # voice reply synthesis
├── embeddings.py        # in-process embedding runtime (EmbeddingBackend) + background model download, LRU cache
├── validation.py        # input validation for cron, config, user actions
├── mcp_core.py          # MCP server for spawn, learn, task, wait, hook, send_message, file_send tools
├── mcp_cron.py          # MCP server for cron tools
├── mcp_discovery.py     # MCP server auto-detection + probing
├── mcp_shared.py        # shared MCP utilities
├── model_tokens.json    # context window sizes for LLM models
├── stats.py             # runtime metrics tracking + daily reports
├── channel_history.py   # group conversation context buffer
├── task.py              # task state machine
├── agent.py             # kiro-cli agent config generator
├── acp/client.py        # ACP JSON-RPC 2.0 over stdio
├── acp/types.py         # protocol types and event models
├── aidlc/               # project management models (Activity, Comment)
├── apps/                # App Kit platform (manifest, manager, registry, routes, SDK)
├── config/loader.py     # ~/.kirocrew/config.json + .env
├── config/schema.py     # JSON Schema generation from config dataclasses
├── eval/                # multi-session eval harness with memory loop
├── slack/gateway.py     # Socket Mode event loop
├── slack/handler.py     # message → LLM → response with tool approval
├── slack/interactions.py # Slack interactive component handling
├── slack/events.py      # Slack event dispatch (Home Tab, file handling, display names)
├── slack/files.py       # Slack file/image/voice attachment handling
├── slack/blocks.py      # Block Kit message builder
├── slack/client.py      # Slack client abstraction (SlackClientOps ABC)
├── dashboard/           # web dashboard backend (aiohttp + SSE + WebSocket)
│   ├── chat.py          # chat session management
│   ├── server.py        # aiohttp server setup
│   ├── state.py         # dashboard state management
│   ├── token_auth.py    # token-based authentication
│   ├── stt_stream.py    # streaming speech-to-text
│   ├── ws.py            # WebSocket handler
│   ├── handlers/        # API handlers (split into 14 focused modules)
│   │   ├── core.py      # core routes (logo, health, config)
│   │   ├── sessions.py  # session CRUD and management
│   │   ├── messaging.py # chat message handling
│   │   ├── files.py     # file upload/download/preview
│   │   ├── cron.py      # cron job management
│   │   ├── memory.py    # memory and lessons
│   │   ├── agents.py    # agent config management
│   │   ├── mcp.py       # MCP server discovery and management
│   │   ├── hooks.py     # webhook management
│   │   ├── prompts.py   # prompt template management
│   │   ├── autonudge.py # autonudge service management
│   │   ├── taskrunner.py # task runner management
│   │   ├── updates.py   # self-update management
│   │   └── usage.py     # Kiro usage analytics
│   ├── handlers_channel.py  # channel page API handlers
│   ├── handlers_project.py  # project page API handlers
│   └── handlers_system.py   # system metrics API handlers

agents/                  # agent config (edit without rebuilding)
skills/                  # skill definitions (edit without rebuilding)
packages/kirocrew-client-py/  # standalone Python SDK client
```

## Configuration

Config: `~/.kirocrew/config.json`

```json
{
  "agent": {
    "approval_mode": "interactive",
    "model": "claude-opus-4.6",
    "bot_name": "",
    "sandbox": "auto",
    "yolo": false,
    "conductor_skill": false,
    "log_level": "WARNING",
    "soft_stop_budget_secs": 10.0,
    "max_subagents": 3,
    "max_channels": 1,
    "max_channel_agents": 3
  },
  "session": { "timeout_secs": 1800, "pool_size": 0, "pool_agent": "" },
  "hooks": {
    "auto_approve_tools": ["ReadFile", "kirocrew-core--*"],
    "auto_replies": [{"pattern": "ping", "reply": "pong 🦞", "exact": true}]
  },
  "slack": {
    "command": "kirocrew",
    "trusted_bot_ids": [],
    "open_channels": [],
    "reactions": { "done": "sparkle" },
    "reactions_enabled": true
  },
  "dashboard": {
    "bot_name": "KiroCrew",
    "avatar": "",
    "restore_sessions": false,
    "merge_queued_messages": false
  },
  "stt": {
    "enabled": false,
    "provider": "whisper",
    "streaming": false,
    "transcribe_region": "us-east-1",
    "language_code": "en-US"
  }
}
```

> **Dashboard port** is **not** a config key — set it with the `KIROCREW_PORT`
> environment variable (default `5476`), or per-invocation with
> `kirocrew gateway --port <n>`. The `dashboard.url` config key is for the
> externally-advertised URL only (remote access / CORS origin).

**Telemetry** (off by default) — `telemetry.enabled` / `telemetry.local_dir` / `telemetry.export_interval_seconds`; when enabled, metrics are written local-only as JSONL under `~/.kirocrew/metrics` (no network egress).

Manage config via CLI: `kirocrew config get [key]`, `kirocrew config set <key> <val>`, `kirocrew config edit`

Credentials: `~/.kirocrew/.env` — `SLACK_APP_TOKEN`, `SLACK_BOT_TOKEN`, `KIROCREW_OWNER_ID`

## Troubleshooting

### Build fails (frontend or backend)

The build has two halves: the React dashboard (`npm`) and the Python backend (`pip`). Common fixes:

```bash
# Frontend build errors → rebuild the dashboard, then re-bundle it
cd website && npm install && npm run build && cd ..
cp -r website/dist src/kiro_crew/static/dist

# Backend install errors → reinstall into a clean venv
python -m venv .venv && source .venv/bin/activate
pip install -e .

# Or do both in one shot
make build
```

If you see Node/GLIBC errors, confirm your Node version (Node 16+ via nvm is the tested baseline).

### `AcpTimeoutError: ACP prompt timed out`

The agent backend didn't respond to the initialize handshake in time. Common causes:

1. **Backend not installed** — confirm `kiro-cli` is on your `PATH` and you are logged in (`kiro-cli login`); `kirocrew doctor` reports its status.
2. **Broken MCP servers in config** — a stale or missing MCP server binary in `~/.kiro/agents/kirocrew.json` can cause the backend to hang during startup. Fix with a clean reinstall:
   ```bash
   kirocrew setup --agent-only --clean
   ```
3. **First launch is slow** — the backend loads MCP servers on first start, which can take over a minute. The init timeout is 120s with one automatic retry.
4. **Network issues** — the agent backend needs to reach its LLM provider. Check connectivity.

Diagnostic steps:

```bash
kiro-cli whoami          # check auth
kiro-cli acp             # test if kiro-cli starts (Ctrl+C to exit)
kirocrew doctor          # full health check
```

### `Failed to spawn warm session` / `Failed to create background session`

Same root cause as `AcpTimeoutError` above — the gateway pre-warms kiro-cli sessions in the background. Follow the AcpTimeoutError steps.

### Slack integration not working

Slack is optional — KiroCrew works fine in dashboard-only mode without it. If you want Slack, see [SLACK_SETUP.md](../SLACK_SETUP.md) for the full setup guide.

Tokens are stored in `~/.kirocrew/.env`. If `kirocrew doctor` shows Slack as "not configured", that's normal for dashboard-only mode.

### MCP server not working after uninstall

`install_agent()` now validates that MCP server commands exist in PATH before writing to `kirocrew.json`. If you previously had an MCP server that was removed, run:

```bash
kirocrew setup --agent-only          # re-validates and drops missing servers
kirocrew setup --agent-only --clean  # nuclear option: fresh config, no merge
```

## Documentation

| Document | Description |
|----------|-------------|
| [SLACK_SETUP.md](../SLACK_SETUP.md) | Slack app creation, workspace approval, slash commands |
| [CONTRIBUTING.md](../CONTRIBUTING.md) | Development setup, workflow, PR guidelines |
| [AGENTS.md](../AGENTS.md) | AI assistant rules, code style, architecture reference |
| [DEPENDENCIES.md](../DEPENDENCIES.md) | Full dependency list and manual install |
| [CHANGELOG.md](../CHANGELOG.md) | Release history |
| [memory-architecture.md](memory-architecture.md) | Memory system design (preferences, projects, history, plan memory) |
| [security-deep-dive.md](security-deep-dive.md) | Security architecture deep dive |
| [AUTOPILOT_DESIGN_AND_LIFECYCLE.md](AUTOPILOT_DESIGN_AND_LIFECYCLE.md) | Autopilot (orchestrator) design and lifecycle |
| [snapshot-and-restore.md](snapshot-and-restore.md) | Portable snapshot and restore |
| [resource-protection.md](resource-protection.md) | Resource protection and rate limiting |
| [app-kit/getting-started.md](app-kit/getting-started.md) | App Kit developer guide |
| [app-kit/manifest-reference.md](app-kit/manifest-reference.md) | App manifest reference |
| [APP_STORE_GUIDELINES.md](APP_STORE_GUIDELINES.md) | App Store publishing guidelines |
| [MOBILE_ACCESS_SETUP.md](MOBILE_ACCESS_SETUP.md) | Mobile dashboard access (tunnels) |
| [kiro-cli/](kiro-cli/) | kiro-cli documentation (installation, chat, MCP, hooks, skills) |
| [design/SOFT-STOP-DESIGN.md](design/SOFT-STOP-DESIGN.md) | Cooperative soft-stop design |
| [REMOTE_DESKTOP_SETUP.md](REMOTE_DESKTOP_SETUP.md) | 24/7 remote host setup |
| [system-specs/modules/persistent-agent-channels.md](system-specs/modules/persistent-agent-channels.md) | Multi-agent collaboration channels |
| [system-specs/](system-specs/) | Module-level specifications |

## Development

See [AGENTS.md](../AGENTS.md) for guidelines.

```bash
# Formatting & linting
black src tests                                  # auto-fix formatting
isort src tests                                  # sort imports
flake8 src tests                                 # lint
mypy src                                         # type-check

# Tests
pytest                                           # run the test suite
make test                                        # format checks + flake8 + mypy + pytest
```

### Frontend Development

The frontend React SPA lives in `website/`. For local development:

```bash
cd website && npm install && npm run dev
```

Production builds are bundled into `src/kiro_crew/static/dist/` (run `npm run build`, then copy `website/dist` into `src/kiro_crew/static/dist`, or just `make build`).
