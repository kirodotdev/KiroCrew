# KiroCrew

Open-source personal AI agent that runs on your own machine. Chat with it from
Slack, a web dashboard, or the command line; let it run multi-step tasks
unattended, schedule recurring jobs, and remember context across sessions.
**[What's New](CHANGELOG.md)**

```
CLI / Slack DM / Dashboard → KiroCrew → LLM agent (kiro-cli over ACP) + MCP tools
```

KiroCrew drives an LLM through the **`kiro-cli`** agent over the
[Agent Client Protocol](https://github.com/zed-industries/agent-client-protocol)
(ACP). kiro-cli is the only provider — install it and log in, and KiroCrew
talks to it for every session. See [Configuration](#configuration).

## Quick Start

KiroCrew ships as a Python backend (installed via `pip`) plus a React web
dashboard (built with `npm`). Memory and the knowledge library use a local
[Ollama](https://ollama.com) server for embeddings.

> **Platforms: macOS, Linux, and Windows.** macOS/Linux install via `pip` as
> below. **Windows** runs natively from the same Python **source install** —
> CPython 3.12 + a venv + `pip install -e . tzdata` (3.12 because numpy 1.x has
> no 3.13 wheel; `tzdata` because Windows ships no system IANA tz database),
> launched as `python -m kiro_crew gateway`. Cross-platform process / file-lock
> / signal / metrics behavior is routed through `kiro_crew.platform_compat`, so
> macOS + Linux behavior is unchanged. See
> **[docs/WINDOWS_INSTALL.md](docs/WINDOWS_INSTALL.md)** for step-by-step Windows
> setup, per-feature status, and troubleshooting. (`install.ps1` is a separate
> thin-client bootstrapper for `kirocrew cloud`, which runs the gateway on a
> Linux EC2 box — not the native Windows path.)

### 1. Install the backend (pip)

```bash
# From a clone of this repo
git clone https://github.com/kirodotdev/KiroCrew.git
cd kirocrew

# Build the frontend (see step 2) BEFORE installing so the dashboard is bundled
pip install .
# or, for development, an editable install:
pip install -e ".[voice]"   # [voice] adds optional speech-to-text extras
```

This installs the `kirocrew` (and `kirocrew-browse`) commands onto your `PATH`.

### 2. Build the frontend (npm)

The dashboard is a React + Vite SPA in the `website/` directory. Production
builds are bundled into `src/kiro_crew/static/dist/` and served by the backend.

```bash
cd website
npm install
npm run build
# Copy the build output into the package so pip bundles it:
#   cp -r website/dist ../src/kiro_crew/static/dist
```

### 3. Install the agent backend

KiroCrew drives the **`kiro-cli`** agent over ACP. Install `kiro-cli` per its
own docs, make sure it is on your `PATH`, and log in:

```bash
kiro-cli login
```

`kirocrew doctor` reports whether `kiro-cli` is found and logged in.

### 4. Install Ollama (for memory / knowledge embeddings)

```bash
# Install Ollama from https://ollama.com, then pull the embedding model:
ollama pull qwen3-embedding:0.6b      # default
# or the documented fallback:
ollama pull nomic-embed-text
```

Ollama runs at `http://localhost:11434` by default. KiroCrew manages its
lifecycle automatically; you can also run your own server.

### 5. Configure and run

```bash
kirocrew setup                # interactive wizard: data dir, agent, credentials
kirocrew doctor               # verify everything is wired up
kirocrew gateway              # start server → open http://localhost:5476
```

**Dashboard-only mode**: skip Slack tokens during `kirocrew setup` to run
without Slack.

## Installation & Distribution

There are three ways to build and run KiroCrew, from lightest (developer
checkout) to heaviest (double-clickable desktop app). All builds are driven by
the [`Makefile`](Makefile) — plain `pip` + `npm`/Vite + `pytest`, no
proprietary tooling. See [docs/INSTALL.md](docs/INSTALL.md) for the full guide.

### a. From source (development)

Build the dashboard, install the backend into a local virtualenv, then run the
gateway straight from `src/`:

```bash
make build                                   # npm build + venv editable install
PYTHONPATH=src python -m kiro_crew gateway   # → http://localhost:5476
```

### b. Self-contained pip wheel

Produce a wheel that bundles the pre-built dashboard, then install it anywhere
with Python:

```bash
make wheel                # → dist/kirocrew-0.1.0-*.whl (dashboard bundled)
pip install dist/*.whl    # installs the kirocrew / kirocrew-browse commands
kirocrew gateway          # → http://localhost:5476
```

### c. Bundled desktop app

Build a double-clickable desktop app that embeds a python-build-standalone
interpreter + uv-installed deps inside an Electron shell — end users need **no**
Python, pip, npm, or node:

```bash
make desktop              # → website/electron/dist/KiroCrew-*.dmg (macOS)
                          #   or website/electron/dist/KiroCrew-*.AppImage (Linux)
```

See [docs/DESKTOP_APP.md](docs/DESKTOP_APP.md) for the build pipeline and how
the app locates and launches the bundled backend.

### Makefile targets

| Target | What it does |
|--------|--------------|
| `make build` | Build the frontend (npm/Vite) + install the backend into `.venv` |
| `make wheel` | Self-contained pip wheel with the dashboard bundled → `dist/` |
| `make desktop` | Full desktop app — DMG (macOS) / AppImage (Linux) |
| `make test` | Build, then run the `pytest` suite |
| `make clean` | Remove build artifacts, dists, caches |

## What It Does

| Surface | Description |
|---------|-------------|
| **Slack DM** | Chat with your agent in Slack. Each thread = isolated AI session with full tool access |
| **Web Dashboard** | React SPA at localhost:5476 — multi-session chat, memory explorer, cron manager, app store |
| **Desktop App** | Electron wrapper with multi-tab gateway connections and native macOS tabs |
| **CLI** | `kirocrew chat`, `kirocrew run TASK.md`, `kirocrew cron`, `kirocrew spawn` |

### Key Capabilities

- **Autonomous task execution** — run multi-step specs unattended for hours (`kirocrew run`)
- **Cron scheduling** — recurring and one-shot jobs with jitter, timeouts, and timezone support
- **Subagent orchestration** — spawn parallel background agents for independent tasks
- **Persistent memory** — learns preferences, remembers context across sessions
- **Self-learning** — corrections persist as lessons that change future behavior
- **App platform** — install and build apps that extend KiroCrew (App Store + SDK)
- **Security sandbox** — OS-level isolation (namespaces/seatbelt), credential redaction, denied command patterns
- **Governance model** — optional two-level security policy (an enterprise ceiling the running app cannot weaken, intersected with per-surface/app/task profiles) enforced at KiroCrew's own tool gate; `kirocrew policy show|validate|explain`
- **MCP tool ecosystem** — auto-discovers and manages MCP servers (slack-mcp and any MCP server you add)
- **Voice** — optional speech-to-text input + text-to-speech replies (Piper, or AWS via the `voice` extra)
- **Knowledge Library** — ingest docs/code into a searchable graph with SQLite FTS5 + Ollama embeddings
- **System service** — `kirocrew service install` for systemd/launchd with auto-restart

For the full feature list, see [docs/FEATURES.md](docs/FEATURES.md).

## Running 24/7

For always-on operation (Slack bot, cron jobs, task runner):

```bash
kirocrew service install      # systemd (Linux) or launchd (macOS)
kirocrew service status
```

For running on a remote host, see [docs/REMOTE_DESKTOP_SETUP.md](docs/REMOTE_DESKTOP_SETUP.md).

## Configuration

Config: `~/.kirocrew/config.json` — manage via `kirocrew config get/set/edit`

```json
{
  "agent": { "provider": "acp", "approval_mode": "interactive", "sandbox": "auto" },
  "session": { "timeout_secs": 1800, "pool_size": 2 },
  "dashboard": { "bot_name": "KiroCrew" },
  "slack": { "command": "kirocrew" }
}
```

> The dashboard port is set via the `KIROCREW_PORT` env var (default `5476`) or
> `kirocrew gateway --port <n>`, not in config. `dashboard.url` is the
> externally-advertised URL only.

**Provider** — `agent.provider` is `acp`: KiroCrew drives the **`kiro-cli`** ACP
agent over stdio. It is the only provider.

**Embeddings** — `memory.embedding_model` (default `qwen3-embedding:0.6b`) and
`memory.embedding_url` (default `http://localhost:11434`) control the Ollama
server used for memory and knowledge-library search.

Credentials: `~/.kirocrew/.env` — `SLACK_APP_TOKEN`, `SLACK_BOT_TOKEN`, `KIROCREW_OWNER_ID`

## Troubleshooting

### `AcpTimeoutError: ACP prompt timed out`

The agent backend didn't respond to the initialize handshake. Fixes:

1. Confirm the `kiro-cli` backend binary is on your `PATH` and you are logged in (`kiro-cli login`)
2. `kirocrew setup --agent-only --clean` if MCP servers are broken
3. Wait — first launch loads MCP servers and can take >60s
4. `kirocrew doctor` for a full health check

### Memory / knowledge search not working

1. Confirm Ollama is installed and running (`curl http://localhost:11434/api/tags`)
2. Pull the embedding model: `ollama pull qwen3-embedding:0.6b`
3. `kirocrew doctor` reports embedding-server health

### Slack integration not working

Slack is optional — dashboard-only mode works without it. For Slack setup, see
[SLACK_SETUP.md](SLACK_SETUP.md).

### MCP server not working after uninstall

```bash
kirocrew setup --agent-only          # re-validates, drops missing servers
kirocrew setup --agent-only --clean  # fresh config from scratch
```

## Documentation

| Document | Description |
|----------|-------------|
| [docs/INSTALL.md](docs/INSTALL.md) | Build & install guide — the three run methods, Makefile targets, env vars |
| [docs/DESKTOP_APP.md](docs/DESKTOP_APP.md) | Electron desktop app build pipeline and packaging |
| [FEATURES.md](docs/FEATURES.md) | Complete feature reference |
| [DEPENDENCIES.md](DEPENDENCIES.md) | Full dependency list (pip + npm + optional extras) |
| [CHANGELOG.md](CHANGELOG.md) | Release history |
| [AGENTS.md](AGENTS.md) | AI assistant rules and development conventions |
| [SLACK_SETUP.md](SLACK_SETUP.md) | Slack app creation and setup |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development workflow and PR guidelines |
| [docs/REMOTE_DESKTOP_SETUP.md](docs/REMOTE_DESKTOP_SETUP.md) | 24/7 remote host setup |
| [docs/security-deep-dive.md](docs/security-deep-dive.md) | Security architecture |
| [docs/memory-architecture.md](docs/memory-architecture.md) | Memory system architecture (preferences, lessons, knowledge graph) |
| [docs/mcp-architecture.md](docs/mcp-architecture.md) | MCP server discovery and tool management architecture |
| [docs/app-kit/getting-started.md](docs/app-kit/getting-started.md) | App Kit developer guide |
| [docs/system-specs/](docs/system-specs/) | Module-level specifications |

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md) for full guidelines.

```bash
# Backend
pip install -e ".[voice]"
pytest

# Frontend (in website/)
npm install
npm run check          # typecheck + lint + tests
npm run build          # production bundle → website/dist
```

## License

See [LICENSE](LICENSE) if present in this repository.
