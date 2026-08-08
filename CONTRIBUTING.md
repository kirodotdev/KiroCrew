# Contributing to Kiro Crew

Thanks for your interest in contributing! Kiro Crew is an open-source project and
we welcome issues and pull requests.

## Reporting Bugs and Requesting Features

Open a [GitHub issue](https://github.com/kirodotdev/KiroCrew/issues). Before you
do, search the open issues, because the fastest resolution is often a thread that
already exists.

For a bug, what actually helps is a way to reproduce it, the version you are on,
your operating system, and anything unusual about how Kiro Crew is installed or
where it runs. A stack trace beats a description of a stack trace. If it only
happens on one surface, say which one, because the dashboard, the CLI, and a chat
channel take different paths through the code.

For a feature, lead with the problem rather than the design. What you were trying
to do and what stopped you tells a maintainer more than a proposed solution, and
it leaves room for an answer nobody had thought of.

## Finding Something to Work On

Start with the [open issues](https://github.com/kirodotdev/KiroCrew/issues). Issues
carry an `area:` label naming the subsystem they land in — `area: dashboard`,
`area: agents`, `area: cron` and so on — so you can filter to the part of the
codebase you want to work in, and a type label (`bug`, `enhancement`,
`documentation`) telling you what kind of change it is.

Before starting anything substantial, check whether someone is already on it and
comment on the issue saying you are picking it up. For a large change, open an
issue first and get a reaction to the approach. Nobody enjoys declining a
finished pull request that went the wrong direction, and a maintainer can usually
tell you in a paragraph.

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
[`src/kiro_crew/builtin_skills/kirocrew-dev/`](src/kiro_crew/builtin_skills/kirocrew-dev/)
— the canonical definition of how code gets written, tested, and reviewed here:

- **`kirocrew-worktree-dev`** — the HARD RULE workflow: every change in a git
  worktree, the blocking build gates, the built-dist gotcha, preview paths.
- **`prepare-pr`** — drives working-tree changes to a review-ready PR
  (commit → sync → squash → open → poll CI/review bots → fix findings).
- **`babysit`** — same-session monitoring loop that keeps a PR moving through
  CI and review rounds.

An agent contributing to Kiro Crew loads this suite and follows the same
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

The user-facing version of this table — same audiences, more detail on
switching — is [Release channels](README.md#release-channels) in the README.
Keep the two in step.

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

Full detail, including the branch, channel, and RC model behind these steps and the
platform-lane contract: **[docs/build/release.md](docs/build/release.md)**.

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
| Python | ≥ 3.10, `from __future__ import annotations` |
| Logging | `import logging` + `logger = logging.getLogger(__name__)` |
| Async | `asyncio` throughout, `async def` for all I/O |
| Data | `@dataclass` for containers |
| Imports | All at top of file, no in-method imports |
| Naming | Module constants: `UPPER_SNAKE`. Private: `_UPPER_SNAKE` |
| Lint | flake8 (F401 unused imports, N806 lowercase vars, W504); isort + black |
| Types | mypy, `# type: ignore[...]` sparingly |

Full reference: [AGENTS.md](AGENTS.md)

## Documentation (required with every behavior change)

**A change that alters documented behavior must update the docs in the same
commit.** A PR that changes behavior and leaves its doc stale will be sent back:
a doc nobody updated is worse than no doc, because readers still trust it.

1. **Find the one owning doc.** Every subsystem has exactly one, usually under
   `docs/system-specs/modules/`. [AGENTS.md](AGENTS.md)'s routing table maps
   subsystem to doc.
2. **Edit that doc; don't add a second one.** Two docs on one subject diverge,
   and then nobody can tell which is true.
3. **Update the indexes** when you add, move, rename, or delete a doc: the
   directory's own `README.md`, [docs/README.md](docs/README.md), and anything
   linking to it.
4. **No changelogs inside docs.** No `Last Updated:` line, no
   "previously/used to/we now", no PR numbers or SHAs. Git holds history; the doc
   states current behavior in present tense.
5. **Run the gate:** `./scripts/docs-lint.sh` (also a blocking CI job). It catches
   broken internal links, docs no index reaches, directories missing an index, code
   comments citing a doc that does not exist, and a renamed doc whose filename is
   hardcoded in code.

Note that `src/kiro_crew/docs/` is **packaged and read at runtime**: its filenames
are an API (see [its README](src/kiro_crew/docs/README.md)), so renaming a file
there is a code change, and an internal engineering note placed there ships to every
user.

## Extending Kiro Crew

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

## Using AI Tools

Most of us build with coding agents, and you are welcome to. This project exists
because of that kind of work.

You are still the author of your pull request. Before you open it, make sure you
understand the change well enough to explain why it works, defend the design, and
fix it when something breaks later. If you could not walk a reviewer through it
line by line, it is not ready, and a reviewer will find that out faster than you
expect.

Three things make agent-assisted contributions land:

Keep the change small and focused on one thing. A large diff that touches many
areas is harder to review than the same work split into three, and it is the most
common reason a well-intentioned pull request stalls.

Open an issue first for anything significant, so the approach is agreed before you
or your agent spend real time on it.

Read every line before you send it. Delete what is not needed, simplify what is
over-built, and check that the tests exercise the behaviour rather than merely
passing. Trimming your own diff is the single highest-leverage thing you can do to
get it merged.

When your change is ready, the workflow is already codified rather than left to
taste. See Development Skills above: `kirocrew-worktree-dev` covers building and
verifying in a worktree, and `prepare-pr` takes it from there, driving the change
to a review-ready pull request by committing, syncing onto the base, squashing to
the single commit this repo requires, opening or updating the PR, then polling CI
and the review bots and fixing what they find. An agent that loads it follows the
same route a maintainer would, which is why the process holds regardless of who or
what wrote the code. If you are contributing with an agent, point it at that skill
instead of describing the steps yourself.

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

Two things are worth knowing before you start something large.
[GOVERNANCE.md](GOVERNANCE.md) covers who decides what lands and how a
disagreement gets resolved, and [MAINTAINERS.md](MAINTAINERS.md) lists the
people doing it.

Architectural changes get written up as an RFC first, in
[docs/request-for-change/](docs/request-for-change/), so the design can be
argued over before anyone writes the code. That applies to changes to a public
interface, changes other parts of the project would have to build around, and
anything that would be expensive to reverse. Everything else skips it, and a bug
fix should never wait on a design document. If you are unsure which side of the
line your change falls on, open an issue and ask.

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

## Code of Conduct

This project has adopted a [Code of Conduct](CODE_OF_CONDUCT.md). Participating
means following it, and the file names where to report a concern.

## Licensing

Kiro Crew is licensed under the Apache License 2.0. See [LICENSE](LICENSE) for the
full text and [NOTICE](NOTICE) for attribution. Third-party components carry their
own licenses, recorded in [THIRD-PARTY-NOTICES](THIRD-PARTY-NOTICES).

Contributions are accepted under the same license as the project. If your change
adds or updates a third-party dependency, say so in the pull request, because it
affects what has to be recorded in the notices file.
