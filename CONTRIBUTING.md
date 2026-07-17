# Contributing to KiroCrew

Thanks for your interest in contributing! KiroCrew is an open-source project and
we welcome issues and pull requests.

## Prerequisites

- macOS or Linux (Windows is not supported by the `kiro-cli` backend)
- Python ≥ 3.9
- Node.js ≥ 18 and npm (for the frontend)
- The `kiro-cli` agent on your `PATH`, logged in (`kiro-cli login`) — it is the
  only LLM backend (`agent.provider = acp`)
- [Ollama](https://ollama.com) for memory and knowledge-library embeddings

## First-Time Setup

```bash
# 1. Fork the repo on GitHub, then clone your fork
git clone https://github.com/kirodotdev/KiroCrew.git
cd kirocrew

# 2. Build the frontend and bundle it into the package
cd website
npm install
npm run build
cp -r dist ../src/kiro_crew/static/dist
cd ..

# 3. Editable backend install (with optional voice extras)
python -m venv .venv && source .venv/bin/activate
pip install -e ".[voice]"

# 4. Configure and verify
kirocrew setup               # data dir, agent backend, Slack tokens (optional)
kirocrew doctor              # verify everything works
kirocrew gateway             # start server (dashboard + Slack)
```

The dashboard is at `http://localhost:5476`.

**Dashboard-only mode**: skip Slack tokens during `kirocrew setup` to run
without Slack.

## Building

### Backend

```bash
pip install -e ".[voice]"    # installs deps + console scripts
pytest                       # run the test suite
```

### Frontend

The React SPA lives in `website/`. Production builds are bundled into
`src/kiro_crew/static/dist/` and served by the backend.

```bash
cd website
npm install
npm run build                # tsc + vite build → website/dist
```

After building, copy `website/dist` into `src/kiro_crew/static/dist/` so the
backend serves the latest assets (the `pip` build step copies this directory
into the wheel).

## Dev Mode (Isolated Data Directory)

Run a dev gateway alongside production without data or port conflicts:

```bash
# Seed dev data from your real config (optional, safe to re-run)
./dev-seed.sh

# Start the dev backend (port 6777, isolated data)
KIROCREW_HOME=.kirocrew-dev KIROCREW_PORT=6777 kirocrew gateway
```

Browse at `http://localhost:6777`. The backend serves the built frontend assets directly.

| Env var | Purpose | Default |
|---------|---------|---------|
| `KIROCREW_HOME` | Config/data directory override | `~/.kirocrew` |
| `KIROCREW_PORT` | Dashboard port override | `5476` |
| `KIROCREW_KIRO_BIN` | Explicit path to the `kiro-cli` binary (overrides PATH auto-detection) | auto-detected |

If you don't need to run production and dev side by side, omit `KIROCREW_PORT` —
just stop your production gateway first.

### Full-Stack Dev Setup (Backend + Frontend Hot-Reload)

When working on frontend changes, run the Vite dev server alongside the backend
for instant hot-reload without rebuilding:

```bash
# Terminal 1 — start the backend
KIROCREW_HOME=.kirocrew-dev KIROCREW_PORT=6777 kirocrew gateway

# Terminal 2 — start the frontend dev server (hot-reloads .tsx changes)
cd website
KIROCREW_PORT=6777 npm run dev
# → Vite starts at http://localhost:3000, proxies /api/* to backend on port 6777

# Terminal 3 — generate an auth token
KIROCREW_HOME=.kirocrew-dev KIROCREW_PORT=6777 kirocrew token
# → Outputs: http://localhost:6777?token=eyJ...

# Open in browser — replace :6777 with :3000:
# http://localhost:3000?token=eyJ...
# Vite's token proxy plugin handles the auth handshake.
```

**Key points:**

- The backend must be reinstalled or restarted after Python source changes
- The frontend hot-reloads automatically — no rebuild for `.tsx`/`.ts`/`.css` changes
- Always access via `localhost:3000` (Vite) during frontend dev, not `localhost:6777` directly
- If the backend restarts, you may need a new token (sessions expire with the process)

## Releasing New Versions

```bash
# 1. Bump version
#    src/kiro_crew/__init__.py  →  __version__ = "X.Y.Z"

# 2. Update CHANGELOG.md

# 3. Build + test
pytest
cd website && npm run build && cd ..

# 4. Commit + tag + push
git add -A
git commit -m "chore: release X.Y.Z"
git tag vX.Y.Z
git push && git push --tags
```

| File | Field |
|------|-------|
| `src/kiro_crew/__init__.py` | `__version__` (source of truth) |
| `CHANGELOG.md` | New `## [X.Y.Z]` section |

## Project Structure

Key entry points:

| File | Purpose |
|------|---------|
| `src/kiro_crew/cli.py` | CLI entrypoint (argparse) |
| `src/kiro_crew/session.py` | Conversation session management |
| `src/kiro_crew/providers/` | LLM provider layer (claude_code, acp, bedrock) |
| `src/kiro_crew/acp/client.py` | ACP JSON-RPC client (stdio) |
| `src/kiro_crew/slack/gateway.py` | Slack Socket Mode gateway |
| `src/kiro_crew/slack/handler.py` | Message handling, tool approval |
| `src/kiro_crew/dashboard/` | Web dashboard (aiohttp backend) |
| `src/kiro_crew/mcp_core.py` | MCP tools: spawn, learn, task, wait, hook, send_message, file_send |
| `src/kiro_crew/mcp_cron.py` | MCP tools: cron scheduling |
| `src/kiro_crew/context.py` | Context builder (memory, skills, history) |
| `src/kiro_crew/subagent.py` | Subagent lifecycle and timeout |
| `src/kiro_crew/autonudge.py` | Reactive same-session self-nudge service |
| `src/kiro_crew/snapshot.py` | Portable snapshot and restore |
| `src/kiro_crew/apps/` | App Kit platform (manifest, manager, registry, routes) |
| `src/kiro_crew/eval/` | Multi-session eval harness |
| `agents/` | Agent config and system prompt |
| `agents/prompt.md` | Default system prompt — edit to change the agent's base personality and rules |
| `skills/` | On-demand skill definitions (see [skills/README.md](skills/README.md)) |
| `website/` | React + Vite frontend SPA |

## Code Style

| Rule | Standard |
|------|----------|
| Line length | 100 chars (black) |
| Python | ≥ 3.9, `from __future__ import annotations` |
| Logging | `import logging` + `logger = logging.getLogger(__name__)` |
| Async | `asyncio` throughout, `async def` for all I/O |
| Data | `@dataclass` for containers |
| Imports | All at top of file, no in-method imports |
| Naming | Module constants: `UPPER_SNAKE`. Private: `_UPPER_SNAKE` |
| Lint | flake8 (F401 unused imports, N806 lowercase vars, W504); isort + black |
| Types | mypy, `# type: ignore[...]` sparingly |

Full reference: [AGENTS.md](AGENTS.md)

## Extending KiroCrew

- **Skills** — drop markdown files in `skills/` or `~/.kirocrew/skills/`. See [skills/README.md](skills/README.md) for the full format reference
- **MCP tools** — add to `mcp_core.py` or `mcp_cron.py`. Every LLM-facing command must have an MCP tool
- **Hooks** — configure in `~/.kirocrew/config.json`
- **Lessons** — self-learned from corrections, stored in `~/.kirocrew/lessons.jsonl`

## Tests

### Backend Tests

```bash
pytest                       # full suite (pytest-asyncio, pytest-xdist)
pytest -k test_name          # single test
```

| Pattern | Example |
|---------|---------|
| File naming | `test/test_<module>.py` |
| Async tests | `@pytest.mark.asyncio` required |
| Filesystem | `tmp_path` fixture |
| Config | `monkeypatch` for overrides |
| External processes | Always mock the agent backend, never spawn real processes |
| Grouping | `class TestFeatureName:` |

### Frontend Tests

```bash
cd website
npm test                     # vitest (unit/component) + electron tests
npm run check                # typecheck + lint + tests
npm run test:integration     # MSW-based integration tests
npm run test:playwright      # E2E (requires a running backend)
```

## Pull Request Workflow

1. **Fork** the repository on GitHub.
2. **Branch** from `main`:
   ```bash
   git fetch origin
   git checkout -b feat/my-feature origin/main
   ```
3. **Make your change** and add tests (new functions/components should be tested).
4. **Run the checks locally** before opening a PR:
   ```bash
   pytest                                   # backend
   cd website && npm run check && cd ..     # frontend: typecheck + lint + tests
   ```
5. **Commit** using [Conventional Commits](https://www.conventionalcommits.org/)
   (see below), push to your fork, and open a **Pull Request against `main`**.
6. A maintainer will review. Address feedback by pushing additional commits to
   your branch.

## Commit Messages

[Conventional Commits](https://www.conventionalcommits.org/):

```
<type>: <summary>

<body — what and why, not how>
```

Types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`

Rules: imperative mood, lowercase summary, no trailing period, wrap body at 72 chars.

## Questions?

Open a [GitHub issue](https://github.com/kirodotdev/KiroCrew/issues) or start a
discussion in the repository.

## Security Issues

**Do not** report security vulnerabilities through public GitHub issues. See
[SECURITY.md](SECURITY.md) for responsible disclosure instructions.
