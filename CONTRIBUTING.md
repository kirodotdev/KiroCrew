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

- macOS, Linux, or Windows — Windows builds and runs natively from source, with
  the documented feature limits in the [Windows guide](docs/guides/windows-install.md)
- Python ≥ 3.12
- Node.js ≥ 22 (24 LTS recommended) and npm (for the frontend)
- An authenticated ACP backend. Fresh setups use `kiro-cli` on `PATH` after
  `kiro-cli login`; other verified harnesses are selected with `agent.acp_backend`
  and have backend-specific prerequisites in the [install guide](docs/guides/install.md)
- Nothing extra for embeddings — memory and the knowledge library embed in-process, so no daemon to install

## First-Time Setup

The root build target owns frontend dependency installation, SPA staging,
virtualenv creation, and the editable backend install. Do not duplicate those
steps manually:

```bash
# Fork the repo on GitHub, then clone your fork
git clone https://github.com/kirodotdev/KiroCrew.git
cd KiroCrew

make build
source .venv/bin/activate
kirocrew setup               # configure the data home and default agent
kirocrew doctor              # verify the install and backend
kirocrew gateway             # start the dashboard and messaging gateway
```

The dashboard is at `http://localhost:5476`. The [install guide](docs/guides/install.md)
owns the build targets, optional extras, and failure recovery.

On Windows, `.\make.ps1 build` builds the frontend and backend; activate
`.venv\Scripts\Activate.ps1` instead. Read the
[Windows guide](docs/guides/windows-install.md) first because some execution
paths require an explicit opt-in.

**Messaging channels are optional**: the default `kirocrew setup` configures
none, and the dashboard + CLI work without channel credentials. Connect a
channel later from the dashboard, or run `kirocrew setup --slack` for the guided
Slack path.

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
change the workflow, change it THERE. The checked-in workflows and
[CI and review guide](docs/ci/ci-and-reviews.md) are canonical for the gate list;
do not copy a gate count into another document.

## Building

Use `make build` for the reproducible frontend + backend build. The
[install guide](docs/guides/install.md#build-targets) owns each target, dependency
choice, optional extra, and platform-specific command; use the narrower target
listed there only when you intentionally need one surface.

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

- The editable backend must be restarted after Python source changes
- The frontend hot-reloads automatically — no rebuild for `.tsx`/`.ts`/`.css` changes
- Always access via `localhost:3000` (Vite) during frontend dev, not `localhost:6777` directly
- If the backend restarts, you may need a new token (sessions expire with the process)

## Releasing New Versions

The release-branch, candidate, stable, hot-patch, version-stamping, artifact,
and recovery procedures are maintained in the
[release runbook](docs/build/release.md). Follow that runbook rather than copying
commands from this guide; these details are coupled to the current workflows.

Release changes land through normal pull requests—never push directly to
`main` or a release branch. The release PR updates `CHANGELOG.md` according to
the [changelog format](docs/build/changelog.md).

## Project Structure

Key entry points:

| File | Purpose |
|------|---------|
| `src/kiro_crew/cli.py` | CLI entrypoint (argparse) |
| `src/kiro_crew/session.py` | Conversation session management |
| `src/kiro_crew/providers/` | LLM provider layer. ACP only — `agent.provider` is fixed to `acp` |
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

The canonical Python, TypeScript, naming, comment, formatting, and lint rules are
in [Code style](docs/system-specs/common/code-style.md). `AGENTS.md` routes each
subsystem to any additional spec that must be read before editing it.

## Documentation (required with every behavior change)

**A change that alters documented behavior must update the docs in the same
commit.** A PR that changes behavior and leaves its doc stale will be sent back:
a doc nobody updated is worse than no doc, because readers still trust it.

The five steps — find the one owning doc, edit rather than add, update every
index, no changelog narration, run `./scripts/docs-lint.sh` — plus the
`src/kiro_crew/docs/` filenames-are-an-API caveat are in
[The rule for changing docs](docs/README.md#the-rule-for-changing-docs).

## Extending Kiro Crew

- **Skills** — drop markdown files in `skills/` or `~/.kiro/crew/skills/`. See [skills/README.md](skills/README.md) for the full format reference
- **MCP tools** — add to `mcp_core.py` or `mcp_cron.py`. Every LLM-facing command must have an MCP tool
- **Hooks** — configure in `~/.kiro/crew/config.json`
- **Lessons** — self-learned from corrections, stored in `~/.kiro/crew/lessons.jsonl`

## Tests

Use targeted tests while iterating, then run the change-scoped local test gate
before a commit:

```bash
python3 scripts/local-gate.py
```

This is the test portion of the [gate before you commit](AGENTS.md#the-gate-before-you-commit),
not a replacement for its static checks. The full test suite is CI's job unless
a human explicitly requests `python3 scripts/local-gate.py --full`. Test
structure, isolation, worker limits, frontend commands, and platform traps are
canonical in [Testing conventions](docs/system-specs/common/testing-conventions.md)
and [Frontend testing](website/docs/testing.md).

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
the one or two commits this repo allows, opening or updating the PR, then polling CI
and the review bots and fixing what they find. An agent that loads it follows the
same route a maintainer would, which is why the process holds regardless of who or
what wrote the code. If you are contributing with an agent, point it at that skill
instead of describing the steps yourself.

One thing the GitHub UI will get wrong for you: if your branch falls behind the
base while the PR is open, **rebase it, do not merge**. Plain-clicking
**Update branch**, like a local `git merge main`, adds a merge commit, which counts
toward the one-or-two-commit limit above and fails `PR Hygiene` on a PR that was
green a moment earlier. That button's dropdown does carry an **Update with rebase**
option, which is safe; the default click is the trap. Pick that option, or run:

```
git fetch origin
git rebase origin/<base branch>
git push --force-with-lease origin <feature-branch>
```

## Pull Request Workflow

1. **Fork** the repository on GitHub.
2. **Branch** from `main`:
   ```bash
   git fetch origin
   git checkout -b feat/my-feature origin/main
   ```
3. **Make your change** and add tests (new functions/components should be tested).
4. **Run the [gate before you commit](AGENTS.md#the-gate-before-you-commit)**
   before opening a PR; its test step is:
   ```bash
   python3 scripts/local-gate.py
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

### CI checks on your PR

The workflow graph, fork approval path, required and advisory review lanes,
ratchet behavior, and inherited-red guidance are canonical in
[CI and reviews](docs/ci/ci-and-reviews.md). Read that guide before changing a
workflow or reacting to a red baseline gate; do not widen a ceiling or baseline
to hide drift from `main`.

## Commit Messages

[Conventional Commits](https://www.conventionalcommits.org/):

```
<type>: <summary>

<body — what and why, not how>
```

Types the PR-title gate accepts: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`,
`test`, `chore`, `ci`, `build`, `revert`.

Rules: imperative mood, lowercase summary of at most 72 chars, no trailing period,
wrap the body at 72 chars, and one logical change per commit.

## Recognizing Contributions

There is one contributor list, the Contributors block in [README.md](README.md).
Deliberately one: a second table for "other" contributions would rank one kind of
help above another, and split recognition across two places nobody reads twice.

Two things are credited automatically by a daily job: authoring a merged pull
request, and reporting an issue that a merged pull request closed. The job also
credits the linked authors and co-authors of a merged PR's commits, so work that
lands under someone else's PR still reaches its real author. You do not need to
ask for any of these.

Superseding another contributor's PR: when you open a replacement PR that takes
over someone else's work, keep their commits authored as-is (cherry-pick, do not
re-author) and add `Co-authored-by: <login> <id+login@users.noreply.github.com>`
to every commit you write. The daily job reads those trailers, so the original
author is credited even though the replacement PR is authored by you.

The second rule is deliberately about outcome, not volume. Credit follows a report
that changed the product, which is why the job reads each merged PR's closing
references rather than listing every issue — that keeps duplicates, invalid
reports, and issues opened to farm a credit out of the list. It undercounts on
purpose: if a pull request fixed your report without writing a closing keyword,
the link does not exist and the job cannot see it. Ask, and it gets added by hand.

Everything else is credited in the same block on request: a code review, a
translation, an idea, a design, a private security report. Open an issue naming
the person (yourself is fine), and a maintainer records it:

```sh
python3 scripts/update_contributors.py --login <github-login> --name "Display Name"
```

The daily job re-derives the full contributor set and rebuilds its branch
from scratch, but it never rewrites or drops an existing line, so a
manually-added entry survives every later run. A login listed in
`.github/contributors-optout.txt` is never added by the job, which is how a
removal request stays honored.

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
