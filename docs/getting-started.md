# Getting Started with KiroCrew

This guide walks you through installing KiroCrew, running it for the first time,
and understanding the core workflows. By the end you'll have a working gateway
with a web dashboard you can chat with.

## Prerequisites

- **Python 3.9+** (3.12 recommended)
- **Node.js 16+** (for building the dashboard)
- **kiro-cli** — the agent backend (installed via the dashboard on first launch)
- **Ollama** (optional) — for memory and knowledge-library embeddings

## Installation

### Option A: One-line install (recommended)

The fastest path — installs a prebuilt, SHA-256-verified wheel from the release
CDN. No clone, no npm, no build step:

```bash
curl -fsSL https://download.crew.kiro.dev/cli.sh | sh -s -- --channel nightly
```

The installer uses `pipx` when available (else creates a managed venv at
`~/.kiro/crew/venv`), verifies the wheel's checksum against the published
manifest, and records your channel to `~/.kiro/crew/channel`.

To pin a specific version:

```bash
curl -fsSL https://download.crew.kiro.dev/cli.sh | sh -s -- --channel nightly --version 0.1.0.dev20260718
```

### Option B: Build from source

For contributors or when you want the latest code:

```bash
git clone https://github.com/kirodotdev/KiroCrew.git
cd KiroCrew

# 1. Build the frontend dashboard
cd website && npm install && npm run build && cd ..

# 2. Install the Python backend (bundles the dashboard)
pip install -e ".[voice]"    # [voice] adds optional speech-to-text extras
```

This installs the `kirocrew` and `kirocrew-browse` commands onto your PATH.

### Option C: Desktop app

A double-clickable app that bundles Python + deps inside Electron — end users
need no Python, pip, or npm:

```bash
make desktop    # → KiroCrew-*.dmg (macOS) or KiroCrew-*.AppImage (Linux)
```

See [desktop-app.md](desktop-app.md) for details on the build pipeline.

### Windows

Windows is supported natively via `kiro_crew.platform_compat`. Use CPython 3.12
+ a venv + `pip install -e . tzdata`. Launch with `python -m kiro_crew gateway`.
See [windows-install.md](windows-install.md) for the full walkthrough.

## First Run

### 1. Run the setup wizard

```bash
kirocrew setup
```

The wizard configures your data directory, agent backend, and (optionally) Slack
credentials. Skip the Slack tokens to run in **dashboard-only mode**.

### 2. Install the agent backend (kiro-cli)

KiroCrew needs `kiro-cli` to talk to the LLM. On the first dashboard launch,
the **Set up Kiro** page guides you through:

1. Installing kiro-cli on your machine (macOS, Linux, or Windows)
2. Completing device-code sign-in

Alternatively, if you already have kiro-cli installed:

```bash
kiro-cli whoami    # check if logged in
kirocrew doctor    # full health check
```

### 3. Set up embeddings (optional but recommended)

Memory and knowledge search use local embeddings. KiroCrew ships a bundled
llama-cpp-python runtime that downloads the model (~610 MB) automatically on
first start. No setup needed — it just works.

If you prefer Ollama:

```bash
# Install from https://ollama.com, then:
ollama pull qwen3-embedding:0.6b
```

### 4. Start the gateway

```bash
kirocrew gateway    # → http://localhost:5476
```

Open the URL in your browser. You're ready to chat.

## Core Workflows

### Chat (Dashboard)

Open `http://localhost:5476` and start typing. Each chat tab is an independent
AI session with full tool access. The agent can read/write files, run commands,
browse the web, and call any configured MCP tool.

### Chat (Slack)

DM your bot in Slack. Each thread becomes its own session. The agent streams
responses with a typing cursor and supports interactive tool approval via
buttons.

### Chat (CLI)

```bash
kirocrew chat                  # interactive REPL
kirocrew chat "quick question" # one-shot
```

### Run autonomous tasks

Write a task spec in markdown, then hand it off:

```bash
kirocrew run TASK.md           # auto-resume from checkpoint on restart
kirocrew run TASK.md --fresh   # ignore checkpoint, start over
```

The task runner decomposes your spec into steps, executes them, runs tests, and
retries on failure — designed for hours of unattended operation.

### Schedule recurring jobs

```bash
kirocrew cron add "briefing" "give me a morning status report" --cron "0 9 * * 1-5"
kirocrew cron list
```

Jobs run in their own sessions. Results are delivered to your Slack DM or
dashboard.

### Spawn parallel agents

```bash
kirocrew spawn run "research topic A"
kirocrew spawn run "research topic B"
kirocrew spawn list
```

Up to 3 concurrent subagents, each with isolated context.

### Teach it preferences

```bash
kirocrew learn add "always use TypeScript over JavaScript"
kirocrew learn add "prefer pytest over unittest" --category tool
kirocrew learn list
```

Lessons are injected into every future session automatically.

## Configuration

Config lives at `~/.kiro/crew/config.json`. Manage it with:

```bash
kirocrew config get agent.sandbox
kirocrew config set dashboard.bot_name "MyAgent"
kirocrew config edit                    # opens in $EDITOR
```

Key settings:

| Key | Default | Purpose |
|-----|---------|---------|
| `agent.provider` | `"acp"` | Agent backend (always kiro-cli) |
| `agent.sandbox` | `"auto"` | OS-level isolation: `auto`, `strict`, or `off` |
| `agent.approval_mode` | `"interactive"` | Tool approval: `interactive` or `auto` |
| `session.timeout_secs` | `1800` | Idle session timeout |
| `dashboard.bot_name` | `"KiroCrew"` | Name shown in the dashboard |
| `memory.embedding_model` | `"qwen3-embedding:0.6b"` | Ollama model for embeddings |

Dashboard port is set via `KIROCREW_PORT` (default `5476`) or
`kirocrew gateway --port <n>`.

Slack credentials: `~/.kiro/crew/.env` — `SLACK_APP_TOKEN`, `SLACK_BOT_TOKEN`,
`KIROCREW_OWNER_ID`. See [../docs/slack-setup.md](slack-setup.md) for the full
Slack app creation guide.

## Running as a Service

For always-on operation (Slack bot, cron jobs, background tasks):

```bash
kirocrew service install    # systemd (Linux) or launchd (macOS)
kirocrew service status
kirocrew service uninstall
```

For remote hosts (EC2, VPS), see [remote-desktop-setup.md](remote-desktop-setup.md).

## Health Check

```bash
kirocrew doctor
```

Reports the status of: kiro-cli binary, kiro-cli auth, Ollama/embeddings,
Slack tokens, config validity, and MCP servers.

## Troubleshooting

### `AcpTimeoutError: ACP prompt timed out`

The agent backend didn't respond to the handshake. Common causes:

1. **kiro-cli not installed** — run through the dashboard Set up Kiro page
2. **Not logged in** — `kiro-cli login`
3. **Broken MCP config** — `kirocrew setup --agent-only --clean`
4. **First launch is slow** — MCP server loading can take 60+ seconds

### Memory / knowledge search returns nothing

1. Check Ollama: `curl http://localhost:11434/api/tags`
2. Pull the model: `ollama pull qwen3-embedding:0.6b`
3. Or wait — the bundled runtime downloads the model in the background on first start

### Gateway won't start

```bash
kirocrew doctor              # diagnose
kirocrew gateway --port auto # try a random port if 5476 is in use
```

## What's Next

- **[Features Reference](features.md)** — full list of every capability
- **[Project Architecture](project-architecture.md)** — system diagrams and component map
- **[Security Deep Dive](security-deep-dive.md)** — sandbox, governance, and denied commands
- **[App Kit Guide](app-kit/getting-started.md)** — build apps that extend KiroCrew
- **[Contributing](../CONTRIBUTING.md)** — development setup and PR workflow
