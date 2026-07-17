# Getting Started with KiroCrew

## What Is KiroCrew?

KiroCrew is an autonomous AI agent layer that runs on top of the kiro-cli
(KiroACP) backend. It adds persistent memory, scheduled jobs, background
subagents, self-learning, and multi-session orchestration. You interact with it
via Slack DMs or a web dashboard.

## Prerequisites

- **Python 3.10+** and **pip** (backend)
- **Node.js 18+** and **npm** (frontend dashboard)
- **The kiro-cli (KiroACP) backend** — required; must be on your `PATH`
- **macOS or Linux** — Windows is not supported
- *(Optional)* **Ollama** for local vector-memory embeddings

## Installation

### Backend (pip)

```bash
pip install kirocrew
```

Or, from a checkout of the repository:

```bash
pip install -e .
```

### Frontend (npm)

The web dashboard is built with Vite:

```bash
cd website
npm install
npm run build
```

In development, `npm run dev` serves the dashboard with hot reload.

### Agent backend

KiroCrew runs the kiro-cli (KiroACP) backend, which is required. Install
kiro-cli and make sure the `kiro-cli` binary is on your `PATH` — KiroCrew
resolves it from `PATH` at startup. See [Configuration](configuration.md) for
backend options.

## First-Time Setup

```bash
kirocrew setup
```

This interactive wizard:
1. Detects the kiro-cli (KiroACP) backend on your PATH
2. Saves the project directory for future use
3. Installs the agent config to `~/.kiro/agents/kirocrew.json`
4. Prompts for Slack credentials (app token, bot token, owner ID)
5. Optionally sets up `http://kirocrew.localhost:5476` custom domain

### Slack Credentials

You need three values from your Slack app:
- `SLACK_APP_TOKEN` — starts with `xapp-`
- `SLACK_BOT_TOKEN` — starts with `xoxb-`
- `KIROCREW_OWNER_ID` — your Slack user ID (starts with `U`)

> ⚠️ **Use the user ID from the workspace where the bot is installed.** Your
> user ID is different in each Slack workspace.

These are stored in `~/.kirocrew/.env`.

## Starting KiroCrew

### Gateway Mode (Slack + Dashboard)

```bash
kirocrew gateway
```

This starts the full server: Slack Socket Mode listener, web dashboard, cron
scheduler, heartbeat, and auto-update checker. The dashboard opens at
`http://localhost:5476`.

### Chat Mode (CLI only)

```bash
# Interactive chat
kirocrew chat

# Single message
kirocrew chat -m "what's the weather like?"
```

Lightweight mode — no Slack, no dashboard, just a terminal REPL with the LLM.

## Verifying Your Setup

```bash
kirocrew doctor
```

Checks: agent backend installation, project directory, agent config, MCP
tools, Slack credentials, gateway status, and vector memory.

## Updating

```bash
pip install -U kirocrew
```

For a checkout, `git pull` and rebuild the frontend (`cd website && npm run
build`). Clicking "Update Available" in the dashboard topbar runs the update
for source installs.

## Running in the Background

### macOS (Launch Agent)

Create `~/Library/LaunchAgents/com.kirocrew.gateway.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.kirocrew.gateway</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/zsh</string>
        <string>-lic</string>
        <string>kirocrew gateway</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key>
    <string>$HOME/.kirocrew/logs/gateway.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$HOME/.kirocrew/logs/gateway.stderr.log</string>
</dict>
</plist>
```

Then load it:
```bash
mkdir -p ~/.kirocrew/logs
launchctl load ~/Library/LaunchAgents/com.kirocrew.gateway.plist
```

Replace `$HOME` with your actual home path. Uses `/bin/zsh -lic` to load your
shell profile (so `kirocrew` is on PATH). Swap to `/bin/bash` if needed.

### Linux (systemd)

```bash
kirocrew service install   # creates and enables the systemd user service
kirocrew service start
```

## Dev Mode

For development, use isolated data directories:

```bash
export KIROCREW_HOME=.kirocrew-dev
export KIROCREW_PORT=6777
kirocrew gateway
```

This keeps dev data separate from your real `~/.kirocrew`. Useful for running
multiple KiroCrew instances simultaneously — each with its own memory, crons,
and sessions.
