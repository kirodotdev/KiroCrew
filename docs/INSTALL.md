# Installing & Building KiroCrew

This guide covers how to build, install, and run KiroCrew. There are three ways
to do it, from lightest (a developer checkout) to heaviest (a double-clickable
desktop app). All builds are driven by the repo-root [`Makefile`](../Makefile)
and use plain `pip` + `npm`/Vite + `pytest` — there is no proprietary build
tooling.

> **Platforms: macOS, Linux, and Windows.** macOS/Linux use the `Makefile` +
> `setup.sh` path below; Windows runs natively from a Python source install
> (`pip install -e . tzdata`, launched via `python -m kiro_crew gateway`). All
> POSIX-only process/signal/file-lock/metrics calls are routed through
> `kiro_crew.platform_compat`. See
> [WINDOWS_INSTALL.md](WINDOWS_INSTALL.md) for the Windows setup steps.

## Prerequisites

| Requirement | Needed for | Notes |
|-------------|------------|-------|
| **Python 3** | Backend | `pip` install; `make build` creates a `.venv` |
| **Node.js + npm** | Frontend (dashboard) | Builds the React/Vite SPA; also for the desktop app |
| **An agent backend** | Driving the LLM | `kiro-cli` — see below |
| **Ollama** (optional) | Memory / knowledge embeddings | Graceful degradation if absent |

### Agent backend (required)

KiroCrew drives an LLM through the **`kiro-cli`** agent over the
[Agent Client Protocol](https://github.com/zed-industries/agent-client-protocol)
(ACP). It is the only provider (`agent.provider = acp`).

Install `kiro-cli` per its own docs, make sure it is on your `PATH`, and log in:

```bash
kiro-cli login
```

`kirocrew doctor` reports whether `kiro-cli` is found and logged in.

### Ollama (optional — for memory / knowledge embeddings)

Memory and the knowledge library use a local [Ollama](https://ollama.com) server
for embeddings. If Ollama is absent, KiroCrew degrades gracefully (embedding
search is disabled) rather than crashing.

```bash
# Install Ollama from https://ollama.com, then pull the embedding model:
ollama pull qwen3-embedding:0.6b      # default
# or the documented fallback:
ollama pull nomic-embed-text
```

Ollama runs at `http://localhost:11434` by default.

## The three ways to run

### a. From source (development)

Build the dashboard, install the backend into a local virtualenv (`.venv`), and
run the gateway directly from `src/`:

```bash
make build                                   # npm build + editable backend install into .venv
PYTHONPATH=src python -m kiro_crew gateway   # → http://localhost:5476
```

`make build` runs two steps:

1. **`frontend`** — `npm ci` (or `npm install`) + `npm run build` in `website/`,
   then copies `website/dist` into `src/kiro_crew/static/dist` so the backend
   serves the SPA.
2. **`backend`** — creates `.venv` and runs an editable install (`pip install -e .`).

You can also invoke any CLI subcommand the same way, e.g.
`PYTHONPATH=src python -m kiro_crew setup` or
`PYTHONPATH=src python -m kiro_crew doctor`.

### b. Self-contained pip wheel

Produce a wheel that bundles the pre-built dashboard, then install it anywhere
that has Python:

```bash
make wheel                # builds the frontend, then python -m build --wheel → dist/
pip install dist/*.whl    # → installs the kirocrew / kirocrew-browse commands onto PATH
kirocrew gateway          # → http://localhost:5476
```

The wheel is `dist/kirocrew-0.1.0-*.whl`. The dashboard is bundled into the
package via the custom `BuildWithFrontend` build step in
[`setup.py`](../setup.py); the pip install name is **`kirocrew`** (the import
package is `kiro_crew`).

Installed console scripts:

| Command | Entry point |
|---------|-------------|
| `kirocrew` | `kiro_crew.cli:main` |
| `kirocrew-browse` | `kiro_crew.browser.cli:main` |

Optional extras (install with e.g. `pip install kirocrew[voice]`):

| Extra | Adds |
|-------|------|
| `voice` | `boto3`, `amazon-transcribe` for speech-to-text |
| `aws` | `boto3` for AWS integrations |
| `desktop` | `pyinstaller` for building the frozen backend (REMOVED — desktop builds use python-build-standalone + uv) |

### c. Bundled desktop app

Build a double-clickable desktop app that embeds a python-build-standalone
interpreter + uv-installed deps inside an Electron shell. End users need **no**
Python, pip, npm, or node:

```bash
make desktop              # → website/electron/dist/KiroCrew-*.dmg (macOS)
                          #   or website/electron/dist/KiroCrew-*.AppImage (Linux)
```

See [DESKTOP_APP.md](DESKTOP_APP.md) for the full build pipeline (frontend →
python-build-standalone → pip install → electron-builder) and how the app
locates and launches the bundled backend.

## Makefile targets

| Target | What it does |
|--------|--------------|
| `make build` | Build the frontend (npm/Vite) + install the backend into `.venv` |
| `make wheel` | Self-contained pip wheel with the dashboard bundled → `dist/` |
| `make desktop` | Full desktop app — DMG (macOS) / AppImage (Linux) |
| `make test` | Build, then run the `pytest` suite |
| `make clean` | Remove build artifacts, dists, and caches |

Override the Python interpreter with `make PY=python3.12 build`.

## Configure and run

After installing (any of the three methods), set up and verify:

```bash
kirocrew setup            # interactive wizard: data dir, agent, credentials
kirocrew doctor           # verify everything is wired up
kirocrew gateway          # start the server → open http://localhost:5476
```

From a source checkout, prefix with `PYTHONPATH=src python -m kiro_crew` instead
of `kirocrew`.

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `KIROCREW_HOME` | `~/.kiro/crew` | Data directory (config, credentials, databases) |
| `KIROCREW_PORT` | `5476` | Port the gateway / dashboard listens on |

- Config file: `~/.kiro/crew/config.json` (manage via `kirocrew config get/set/edit`).
- Credentials: `~/.kiro/crew/.env` (`SLACK_APP_TOKEN`, `SLACK_BOT_TOKEN`, `KIROCREW_OWNER_ID`).

> **Data home moved under `~/.kiro/`.** KiroCrew now stores its data in
> `~/.kiro/crew` (was the top-level `~/.kirocrew`), sharing the `~/.kiro/` base
> with other Kiro-family apps. An existing `~/.kirocrew` install migrates
> automatically on first launch — its data (config, credentials, session
> history, databases) is copied into `~/.kiro/crew` and the old directory is
> renamed to `~/.kirocrew.archived` as a rollback copy. Re-downloadable bulk
> content (the embedding `models/` and rebuildable `cache/`) is **not** copied
> or archived — the new home regenerates it on first start. Set `KIROCREW_HOME`
> to relocate the data home (e.g. outside `~/.kiro/` entirely).
>
> **Rolling back (downgrade).** A release older than the move knows nothing of
> `~/.kiro/crew`; on downgrade it finds no `~/.kirocrew` and starts empty — this
> looks like data loss but is not. To roll back, first stop KiroCrew, then
> restore the archived copy:
>
> ```bash
> mv ~/.kirocrew.archived ~/.kirocrew   # the old release reads this again
> ```
>
> The old release re-downloads the embedding model on its next start. The
> archive is locked to your user account and its credential files (`.env`, signing
> keys) are **auto-removed after 7 days** — so roll back within that window to
> keep the archived tokens, or just re-enter your Slack tokens on the downgraded
> release. Once you are satisfied the new `~/.kiro/crew` home works, delete the
> whole archive to reclaim disk (`rm -rf ~/.kirocrew.archived`). `kirocrew doctor`
> reports the archive's size and this cleanup command under **Data Home**.

> **Note:** `KIROCREW_PORT` is an environment variable (validated at CLI entry),
> not a config key; it sets the port the gateway / dashboard binds to. You can
> also pass `--port` on the CLI to override it. The `dashboard.url` config key is
> only for advertising a remote URL.

## Troubleshooting

For runtime issues (ACP handshake timeouts, embedding/memory search, Slack,
MCP server cleanup), see the **Troubleshooting** section of the
[README](../README.md#troubleshooting). A quick health check is always:

```bash
kirocrew doctor
```
