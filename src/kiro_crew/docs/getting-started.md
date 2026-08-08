# Getting Started with Kiro Crew

Kiro Crew is an autonomous AI agent layer that runs on your own machine on top of
the kiro-cli (KiroACP) backend. It adds persistent memory, scheduled jobs,
background subagents, self-learning, and multi-session orchestration, and you
talk to it from a web dashboard, from Slack DMs, or from the terminal.

## Prerequisites

| Requirement | Needed for | Floor |
|-------------|------------|-------|
| **Python** + pip | Backend | `>= 3.10` |
| **Node.js** + npm | Building the dashboard from source | `20` or `>= 22` |
| **`kiro-cli`** | Driving the LLM | Required, on your `PATH` |

Node is only needed to *build* the dashboard. The prebuilt wheel, the macOS DMG,
and the Linux AppImage all ship the dashboard already bundled, so if you install
one of those you need neither Node nor a compiler.

**Platforms: macOS, Linux, and Windows.** Windows runs natively from a Python
source install and is launched as `python -m kiro_crew gateway`.

Embeddings need no setup step. They run in-process, and on first start the
gateway downloads the embedding model (about 610 MB) in the background and
verifies it against a pinned sha256. Until it lands, memory search falls back to
keyword search and picks up semantic search automatically with no restart.

## Installation

### Prebuilt wheel from the release CDN (fastest)

```bash
curl -fsSL https://download.crew.kiro.dev/cli.sh | sh
```

This installs a signed, sha256-verified `kirocrew` wheel. `stable` is the
default channel; pass `--channel insider` or `--channel nightly` to track a
faster one, or `--version X.Y.Z` to pin an exact release. The installer uses
`pipx` when available, otherwise it creates a managed venv and symlinks
`kirocrew` into `~/.local/bin`.

`pip install kirocrew` alone does **not** work: Kiro Crew is not published to
PyPI. To install with pip directly, point it at a release channel's index:

```bash
pip install --pre kirocrew \
  --extra-index-url https://updates.crew.kiro.dev/feed/stable/simple/
```

`--extra-index-url` (not `--index-url`) is required, because the channel index
carries only `kirocrew` and pip still needs PyPI to resolve its dependencies.

### From a source checkout

```bash
git clone https://github.com/kirodotdev/KiroCrew.git
cd Kiro Crew
cd website && npm install && npm run build && cd ..
pip install -e ".[voice]"       # [voice] adds the optional speech-to-text extras
```

The dashboard has to be built before the backend install, because the built
`website/dist` is staged into the package and served by the gateway. `make
build` does both steps plus a `.venv`.

### Agent backend: `kiro-cli` (required)

`kiro-cli` is the only provider (`agent.provider` is fixed to `acp`). Install it
per its own docs, make sure the binary resolves on your `PATH`, and log in:

```bash
kiro-cli login
```

On the first dashboard launch, the Set up Kiro page walks through installing the
CLI and completing sign-in.

## First-Time Setup

```bash
kirocrew setup
```

This interactive wizard detects `kiro-cli` on your PATH, saves the project
directory so Kiro Crew works from any working directory, installs the agent
config to `~/.kiro/agents/kirocrew.json`, registers the browser MCP proxy,
prompts for Slack credentials, and offers to set up the
`http://kirocrew.localhost:5476` custom domain.

To actually browse, turn on **Browser Mode** in Settings → Browser. Enabling it
downloads and sets up Playwright (`@playwright/mcp` plus the selected engine's
browser binary, bootstrapping Node if needed); browsing is then default-on
whenever Browser Mode stays enabled.

Use `kirocrew setup --agent-only` to reinstall just the agent config and skip
the credential prompts.

### Slack Credentials (optional)

Slack is optional. To use it you need three values from your Slack app:

- `SLACK_APP_TOKEN` starts with `xapp-`
- `SLACK_BOT_TOKEN` starts with `xoxb-`
- `KIROCREW_OWNER_ID` is your Slack user ID (starts with `U`)

Use the user ID from the workspace where the bot is installed: your user ID is
different in every Slack workspace. Only the owner is authorized to interact
over Slack.

These are stored in `~/.kiro/crew/.env`.

## Starting Kiro Crew

### Gateway mode (dashboard + Slack)

```bash
kirocrew gateway
```

This starts the full server: web dashboard, Slack Socket Mode listener, cron
scheduler, heartbeat, and update checker. The dashboard is at
`http://localhost:5476`.

### Chat mode (CLI only)

```bash
kirocrew chat                            # interactive REPL
kirocrew chat -m "what's the weather like?"   # single message
```

Lightweight mode: no Slack, no dashboard, just a terminal conversation.

## Verifying Your Setup

```bash
kirocrew doctor
```

Reports the resolved platform edition, the `kiro-cli` binary and login state,
the project directory, the agent config and its MCP entries, Slack credentials,
gateway status, and the embedding runtime and model. It repairs missing MCP
entries where it can, and prints a specific fix hint for anything it cannot.

## Updating

```bash
kirocrew update
```

For a source checkout this pulls, rebuilds the frontend, reinstalls the package,
and restarts in place. Clicking "Update Available" in the dashboard topbar runs
the same path.

## Running in the Background

```bash
kirocrew service install     # launchd LaunchAgent on macOS, systemd unit on Linux
kirocrew service status
kirocrew service uninstall
```

The service survives an SSH disconnect, restarts on crash, and starts on boot.
On Linux the install prompts for `sudo` once to write the unit; the gateway
itself then runs as your own user. macOS needs no `sudo`.

Tail its output with `kirocrew logs -f`.

## Dev Mode

```bash
export KIROCREW_HOME=.kirocrew-dev
export KIROCREW_PORT=6777
kirocrew gateway
```

`KIROCREW_HOME` gives the instance its own data directory, so dev memory, crons,
and sessions never touch your real `~/.kiro/crew`. `KIROCREW_PORT` lets a second
gateway run alongside the first. The two together are what make it safe to run
several Kiro Crew instances at once.
