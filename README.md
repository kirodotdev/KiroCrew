# KiroCrew

Open-source personal AI agent that runs on your own machine. Chat from Slack, a
web dashboard, or the CLI; run multi-step tasks unattended; schedule cron jobs;
persist memory across sessions. **[What's New](CHANGELOG.md)**

```
CLI / Slack / Dashboard → KiroCrew gateway → kiro-cli (ACP) → LLM + MCP tools
```

KiroCrew orchestrates the **[kiro-cli](https://kiro.dev)** agent over the
[Agent Client Protocol](https://github.com/zed-industries/agent-client-protocol)
(ACP), adding multi-session management, persistent memory, scheduling, and a web
UI on top of it.

## Quick Start

```bash
# One-line install (macOS / Linux) — prebuilt wheel, SHA-256 verified
curl -fsSL https://download.crew.kiro.dev/cli.sh | sh -s -- --channel nightly

# Then configure and launch
kirocrew setup        # interactive wizard
kirocrew gateway      # opens http://localhost:5476
```

Or build from source:

```bash
git clone https://github.com/kirodotdev/KiroCrew.git && cd KiroCrew
cd website && npm install && npm run build && cd ..
pip install -e .
kirocrew setup && kirocrew gateway
```

The dashboard guides first-time users through installing kiro-cli and completing
sign-in. Run `kirocrew doctor` to verify everything is wired up.

### Docker: run the gateway as a container

For always-on servers (Slack/Discord bots, remote dashboards) the gateway
ships as a multi-arch image on GHCR:

The package is private for now. Authenticate to GHCR with an account that has
package access before pulling; see **[docs/DOCKER.md](docs/DOCKER.md)**.

```bash
docker run -d --name kirocrew \
  -p 127.0.0.1:5476:5476 \
  -v kirocrew-home:/home/kirocrew \
  ghcr.io/kirodotdev/kirocrew:stable
```

See **[docs/docker.md](docs/docker.md)** for first-run login, channel
credentials, tags (`stable` / `insider` / `nightly` / pinned versions), and
the container security model.

See the **[Getting Started guide](docs/getting-started.md)** for the full
walkthrough, including Ollama setup for memory embeddings and all installation
options.

> **Platforms:** macOS, Linux, and Windows (native). See
> [docs/windows-install.md](docs/windows-install.md) for Windows-specific steps.

## What It Does

| Surface | Description |
|---------|-------------|
| **Web Dashboard** | Multi-session chat, memory explorer, cron manager, app store |
| **Slack DM** | Each thread = isolated AI session with full tool access |
| **Desktop App** | Electron wrapper — no Python/npm needed for end users |
| **CLI** | `kirocrew chat`, `kirocrew run TASK.md`, `kirocrew cron`, `kirocrew spawn` |

### Key Capabilities

- **Autonomous tasks** — run multi-step specs unattended (`kirocrew run TASK.md`)
- **Cron scheduling** — recurring jobs with timezone, jitter, and per-job timeouts
- **Subagent orchestration** — spawn parallel background agents
- **Persistent memory** — preferences, projects, and daily history survive restarts
- **Self-learning** — corrections persist as lessons injected into all future sessions
- **App platform** — build and install apps that extend KiroCrew (App Store + SDK)
- **Security** — OS sandbox, credential redaction, 137 denied-command patterns, governance model
- **MCP tools** — auto-discover and manage any MCP server
- **Knowledge Library** — ingest docs/code into a searchable graph
- **Voice** — optional STT/TTS (Piper local, or AWS via `[voice]` extra)

## Running 24/7

```bash
kirocrew service install    # systemd (Linux) or launchd (macOS)
kirocrew service status
```

For remote hosts, see [docs/remote-desktop-setup.md](docs/remote-desktop-setup.md).

## Configuration

Config lives at `~/.kirocrew/config.json` — manage via `kirocrew config get/set/edit`.

```json
{
  "agent": { "provider": "acp", "approval_mode": "interactive", "sandbox": "auto" },
  "session": { "timeout_secs": 1800 },
  "dashboard": { "bot_name": "KiroCrew" },
  "slack": { "command": "kirocrew" }
}
```

Dashboard port: `KIROCREW_PORT` env var (default `5476`).
Credentials: `~/.kirocrew/.env` — `SLACK_APP_TOKEN`, `SLACK_BOT_TOKEN`, `KIROCREW_OWNER_ID`.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `AcpTimeoutError` | Confirm `kiro-cli` is on PATH and logged in; `kirocrew setup --agent-only --clean` to reset MCP config |
| Memory search not working | Install Ollama + `ollama pull qwen3-embedding:0.6b`; run `kirocrew doctor` |
| Slack not connecting | Slack is optional — dashboard works without it. See [SLACK_SETUP.md](SLACK_SETUP.md) |
| MCP server broken | `kirocrew setup --agent-only --clean` rebuilds from scratch |

## Documentation

| Document | What it covers |
|----------|---------------|
| **[docs/getting-started.md](docs/getting-started.md)** | Full installation walkthrough and first steps |
| [docs/features.md](docs/features.md) | Complete feature reference |
| [docs/project-architecture.md](docs/project-architecture.md) | System architecture with diagrams |
| [docs/install.md](docs/install.md) | All build/install methods (source, wheel, desktop app) |
| [docs/security-deep-dive.md](docs/security-deep-dive.md) | Security architecture |
| [docs/memory-architecture.md](docs/memory-architecture.md) | Memory system design |
| [docs/mcp-architecture.md](docs/mcp-architecture.md) | MCP server management |
| [docs/app-kit/getting-started.md](docs/app-kit/getting-started.md) | App Kit developer guide |
| [SLACK_SETUP.md](SLACK_SETUP.md) | Slack app creation and setup |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development workflow and PR guidelines |
| [CHANGELOG.md](CHANGELOG.md) | Release history |

## Development

```bash
pip install -e ".[voice]"    # editable backend install
cd website && npm install    # frontend deps

# Quality gate
black src && isort src && flake8 src && mypy src && pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md) for full guidelines.

## License

See [LICENSE](LICENSE).
