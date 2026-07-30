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

## Development Skills (agents and humans)

The contributor workflow is codified as agent-loadable skills in
[`skills/kirocrew-dev/`](skills/kirocrew-dev/) — the canonical definition of
how code gets written, tested, and reviewed here:

- **`kirocrew-worktree-dev`** — the HARD RULE workflow: every change in a git
  worktree, the blocking build gates, the built-dist gotcha, preview paths.
- **`prepare-pr`** — drives working-tree changes to a review-ready PR
  (commit → sync → squash → open → poll CI/review bots → fix findings).
- **`babysit`** — same-session monitoring loop that keeps a PR moving through
  CI and review rounds.

An agent contributing to KiroCrew loads this suite and follows the same
worktree → build gate → prepare-pr → review loop human contributors use, so
the PR process stays consistent regardless of who is writing the code. If you
change the workflow, change it THERE — those files are the single source of
truth (with `.github/workflows/ci.yml` canonical for the gate list).

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
| `KIROCREW_HOME` | Config/data directory override | `~/.kiro/crew` |
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

### The model

`main` is always the latest code, and deliberately not stable. Feature releases
are cut as a **release branch** off `main` on 0.1 increments (`0.1.0` → `0.2.0`
→ `0.3.0`).

Once a branch is cut, **bug fixes for that release go on the release branch, not
on `main`.** Each one produces a new release candidate — `0.2.0-rc.1`,
`-rc.2`, … — published to the insider channel. **Stable is the last RC we judge
stable enough, promoted by tagging that RC's commit — never rebuilt.** So
`0.2.0-rc.5` becomes stable `0.2.0`: same commit, same bytes, a new tag.

Hot patches bump the patch digit (`0.2.0` → `0.2.1`) and are also cut from the
release branch.

After each stable cut, do two things: **bump `main` by 0.1** (to `0.3.0`) so
nightlies sort above what just shipped, and **merge the branch's fixes back into
`main`** so they aren't stranded on the branch.

### Channels

| Channel | Built from | Who it's for |
|---------|-----------|--------------|
| nightly | `main` | us and contributors |
| insider | release branch, RC tags | power users testing ahead |
| stable | the promoted insider | everyone (client default) |

Nightly installs **side by side** as its own app. Insider and stable are two
update lanes of **one** production app, switchable in Settings.

### Cutting a release

```bash
# 1. Branch off main
git switch -c release/0.2.0 origin/main
git push -u origin release/0.2.0

# 2. Tag RCs on the branch as fixes land → each publishes to insider
git tag -a v0.2.0-rc.1 -m "0.2.0 rc1" && git push origin v0.2.0-rc.1
#    ... fixes land on release/0.2.0 ... then v0.2.0-rc.2, -rc.3, …

# 3. Promote: tag the good RC's COMMIT with a bare version → stable
git tag -a v0.2.0 -m "release 0.2.0" <rc-commit-sha>
git push origin v0.2.0

# 4. Bump main to 0.3.0 (PR), and merge the branch's fixes back into main

# Hot patch: fix on the release branch, then
git tag -a v0.2.1 -m "release 0.2.1" && git push origin v0.2.1
```

Update `CHANGELOG.md` with a `## [X.Y.Z] — YYYY-MM-DD` section as part of the
release (see AGENTS.md → "Release Changelog" for the format), and land the
changelog and any version bump through a normal PR — never push to `main` or a
release branch directly.

### How builds are triggered

**Nightly** runs on a schedule every night and can be kicked off on demand at any
time. **Insider and stable are triggered by pushing a version tag** — an RC tag
publishes to insider, a plain version tag publishes to stable.

The release branch, the RC numbering, the promote decision, and the back-merge
are all **human process**. The pipeline only reacts to the tag.

Each build ships a signed and notarized macOS app, a Linux AppImage, a pip
wheel, and a Docker image. A channel's update feed is repointed **last**, after
its artifacts are verified downloadable, and clients only install with the
user's consent. Windows builds but is not yet signed or published.

**There is no rollback — we roll forward by cutting a new version.** Published
CDN keys are immutable and are never overwritten.

### Bumping the in-code version

The in-code version governs **non-tag** builds — nightly and local/source
installs. A tagged release overrides all three manifests at build time, so this
is what makes nightlies read as previews of the *next* release:

| File | Field |
|------|-------|
| `src/kiro_crew/__init__.py` | `__version__` — the source of truth |
| `pyproject.toml` | `[project] version` — what the wheel carries |
| `website/electron/package.json` | `version` — the updater's version compare |

Keep it a bare `X.Y.Z`: `nightly.yml` builds both a semver and a PEP 440 stamp
from it, and a suffixed base (`.dev0`) produces invalid versions.

### One trap worth knowing

Any two prerelease tags sharing a base and a trailing number collapse onto the
same PEP 440 wheel version — `v0.2.0-rc.1` and `v0.2.0-insider.1` both map to
`0.2.0rc1`. The second publish then fails as a republish of an immutable key, so
**stick to one prerelease convention (`-rc.N`) per base version.**

Full detail: [docs/release-automation.md](docs/release-automation.md) (as-built
operational reference for the pipeline) and
[docs/release-process-design.md](docs/release-process-design.md) (design +
platform-lane contract).

For the branch, channel, and RC model behind these steps — where the tag goes
and how insider becomes stable — see
**[docs/release-process.md](docs/release-process.md)**.

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

- **Skills** — drop markdown files in `skills/` or `~/.kiro/crew/skills/`. See [skills/README.md](skills/README.md) for the full format reference
- **MCP tools** — add to `mcp_core.py` or `mcp_cron.py`. Every LLM-facing command must have an MCP tool
- **Hooks** — configure in `~/.kiro/crew/config.json`
- **Lessons** — self-learned from corrections, stored in `~/.kiro/crew/lessons.jsonl`

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

### CI checks on your PR (forks vs. direct branches)

GitHub deliberately withholds repository secrets and OIDC credentials from
workflows triggered by **pull requests opened from a fork**. Three of our
checks need those credentials to reach Amazon Bedrock, so their behaviour
depends on *where your branch lives*:

| Check | Fork PR | Branch pushed to `kirodotdev/KiroCrew` |
| --- | --- | --- |
| **Opus 5 Review** | Skipped (neutral — not a failure) | Runs |
| **GPT 5.6 Review** | Skipped | Runs |
| **Design Review** | Skipped | Runs |
| Tests, lint, typecheck, CodeQL, coverage, build | Run normally | Run normally |

- **Opening from a fork (the default for most contributors):** the three AI
  reviews are **skipped, not failed** — and this is identical for *everyone*,
  regardless of permission level. A maintainer who opens a PR from their own
  personal fork gets exactly the same skip; write access does not change it.
  A skipped review does **not** block your PR and there is nothing for you to
  fix: just make sure the credential-free checks (tests, lint, typecheck,
  CodeQL, coverage, build) are green. A maintainer runs the AI review on their
  side (or re-pushes your branch to the upstream repo) and reviews manually.
- **Getting the AI reviews to run** depends only on *where the branch lives*,
  never on who you are: the branch has to be on `kirodotdev/KiroCrew` itself,
  not on a fork. Pushing a branch directly to the upstream repo requires write
  access — so if you have it, push there and open the PR from that branch to
  get the full suite. Without write access, the fork path above is the correct
  and only route, by design.

If your only red checks are the AI reviews on a fork PR, there is nothing for
you to fix — flag it to a maintainer.

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
