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
- Python ≥ 3.10
- Node.js ≥ 22 (24 LTS recommended) and npm (for the frontend)
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
cd electron && npm ci && cd ..       # desktop sub-package deps (npm test needs them)
cd ..

# 3. Editable backend install (with dev/test tooling)
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
# Optional voice extras (local speech-to-text): pip install -e ".[dev,voice]"

# 4. Configure and verify
kirocrew setup               # data dir, agent backend (channels connect later)
kirocrew doctor              # verify everything works
kirocrew gateway             # start server (dashboard + messaging channels)
```

The dashboard is at `http://localhost:5476`.

On Windows, `.\make.ps1 build` does steps 2 and 3 in one command (the venv lands
in `.venv\Scripts\`, and `Activate.ps1` replaces `source .venv/bin/activate`).
Read the [Windows guide](docs/guides/windows-install.md) first — a few features
need an explicit opt-in there.

**Messaging channels are optional**: the default `kirocrew setup` configures
none, and the dashboard + CLI work without any channel credentials. Connect
Slack, Discord, Telegram, Teams, Webex, WeCom, or WeChat later, or run
`kirocrew setup --slack` for the guided Slack path.

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
truth (with `.github/workflows/ci.yml` plus
`.github/workflows/fast-gate.yml` canonical for the gate list — the eleven cheap
blocking gates live in the second one so a red gate can skip the expensive matrix
instead of racing it).

## Building

### Backend

```bash
pip install -e ".[dev]"      # installs deps + console scripts + test tooling
# Optional voice extras (local speech-to-text): pip install -e ".[dev,voice]"
pytest                       # run the test suite
```

### Frontend

The React SPA lives in `website/`. Production builds are bundled into
`src/kiro_crew/static/dist/` and served by the backend.

```bash
cd website
npm install
npm run build                # tsc + vite build → website/dist
cd electron && npm ci && cd ..       # desktop sub-package deps (npm test needs them)
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
stable enough, promoted by tagging that RC's commit — never rebuilt.** The RC
run records one immutable promotion bundle (wheel/sdist, AppImage, notarized
zip/DMG, and OCI manifest digest). A bare `v0.2.0` tag on that exact commit
resolves the newest successful `0.2.0-*` run, verifies the GitHub artifact's
API-recorded digest plus every file digest in its manifest, and only then moves
stable pointers/tags to those bytes.

Because changing an embedded version changes and invalidates the tested bytes,
the promoted binaries retain the selected RC's embedded version; the bare git
tag, GitHub Release, and stable channel are the final release identity. If the
record is missing or its 90-day artifact retention elapsed, promotion fails
closed: cut and validate a fresh RC rather than rebuilding stable.

Hot patches bump the patch digit (`0.2.0` → `0.2.1`) from the release branch and
must also have a successful prerelease candidate before the bare stable tag.

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

# 3. Promote: tag the good RC's EXACT COMMIT with a bare version → stable.
#    release.yml resolves that successful RC run's immutable promotion bundle;
#    it does not invoke either build workflow on the bare tag.
git tag -a v0.2.0 -m "release 0.2.0" <rc-commit-sha>
git push origin v0.2.0

# 4. Bump main to 0.3.0 (PR), and merge the branch's fixes back into main

# Hot patch: fix on the release branch, cut/test v0.2.1-rc.1 first, then
# put bare v0.2.1 on that candidate's exact commit and push it.
```

Update `CHANGELOG.md` with a `## [X.Y.Z] - YYYY-MM-DD` section as part of the
release (see AGENTS.md → "Release Changelog" for the format), and land the
changelog and any version bump through a normal PR — never push to `main` or a
release branch directly.

### How builds are triggered

**Nightly** runs on a schedule every night and can be kicked off on demand at any
time. **Insider and stable are triggered by pushing a version tag** — an RC tag
builds and publishes to insider, while a plain version tag promotes the exact
recorded RC artifacts to stable without rebuilding.

The release branch, the RC numbering, the promote decision, and the back-merge
are all **human process**. The pipeline reacts to the tag, but the stable path
also requires the successful same-commit prerelease record and fails closed if
it cannot prove that record's immutable digest.

A nightly or prerelease build produces a signed and notarized macOS app, a Linux
AppImage, a pip wheel, and a Docker image. Stable republishes/retags those exact
candidate bytes. A channel's update feed is repointed **last**, after its
artifacts are verified downloadable, and clients only install with the user's
consent. Windows builds but is not yet signed or published.

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

Keep it a bare `X.Y.Z` **on `main`**: `nightly.yml` builds both a semver and a
PEP 440 stamp from it, and a suffixed base (`.dev0`) produces invalid versions.

On an **insider release branch** the in-code version instead carries the RC, so
a source/dev checkout reads as the candidate it is. All three files use the
**same dual-valid spelling** `X.Y.Z-rc.N` (e.g. `0.4.0-rc.4`): it is valid
SemVer for `package.json` **and** valid (non-canonical) PEP 440, which pip and
setuptools normalize to `X.Y.ZrcN`. Do not use the canonical PEP 440 spelling
(`0.4.0rc4`) in `__init__.py` — `packaging/build-desktop.sh` greps `__version__`
straight into electron-builder's `extraMetadata.version`, which rejects
non-SemVer and kills a local `make desktop`. The tag still overrides all three
at build time (see `docs/build/release.md` → "Version numbering policy").

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
pytest test/test_agent.py    # one file — what you want most of the time
```

The suite is large (56k+ tests) and runs in parallel. Each worker needs about
1.5 GiB, mostly just to collect the suite, so **on a laptop with 8–16 GiB of RAM a
full run does not fit alongside a browser.** You do not have to work that out: the
worker count is bounded by how much memory is actually free, and if it gets clamped
the run says so in one line. If it clamps to one or two workers, run the subset you
are changing instead — a full-suite checkpoint is what CI is for. Details and the
override knobs: [testing-conventions](docs/system-specs/common/testing-conventions.md).

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
the one or two commits this repo allows, opening or updating the PR, then polling CI
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
| **Opus 4.8 Review** | Skipped (neutral — not a failure) | Runs |
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

### Ratchet and baseline gates (why a check can fail for something you did not touch)

Some of our checks are not "does this pass or fail" tests but *ratchets*: a gate
that records the current count of a thing we are burning down and then fails if
that count grows, so the number is only ever allowed to shrink. The idea is that
we never make a known problem worse, and every PR either holds the line or pays
some of it down. A few examples (`.github/workflows/ci.yml` and
`.github/workflows/fast-gate.yml` stay canonical for the full list — most of the
diff-scoped ratchets are in the second — so this is illustration, not an
inventory):

- the black formatting **baseline** (`.github/black-baseline.txt`) and the
  config-baseline snapshot (`config-baseline.json`, checked by
  `test/test_config_baseline.py`);
- the gate-side log-site census in `test/test_security_posture.py`, which pins
  the exact count of sites it expects and fails if the real count drifts;
- the frontend eslint **ceiling** in the frontend-lint job. This one has already
  been burned down to a hard zero, which is what a ratchet is aiming at: with
  nothing recorded there is nothing to drift, and any warning a change
  introduces fails. `ci.yml` holds the value, and it is the only place that may
  — see [ci-and-reviews.md](docs/ci/ci-and-reviews.md).

The confusing part is that one of these can go red on a PR whose own diff is
completely innocent. That happens because your PR's CI does not run against your
branch in isolation. It runs against `merge(branch, main)`, so it inherits
main's current state along with your changes. If a ratchet-affecting change
landed on main (say a cleanup that removed a warning or a log site but left the
recorded count behind) and main's own CI was superseded or surface-skipped
before that ratchet lane reported, then your PR is simply the first place the
verdict actually renders. The gate is red because of drift on main, not because
of anything in your diff.

The Main Ratchet Audit workflow (`.github/workflows/main-ratchet-audit.yml`)
runs these gates directly on every push to main, non-cancellable and on both
surfaces, and opens a tracking issue (labeled `ratchet-audit`, titled "Main
ratchet drift detected") when it finds drift. It judges the **whole tree**,
where your PR's copy of the same gate judges only the files your diff touches —
so the audit can name a file no pull request would ever have flagged, which is
how the drift got in. That closes most of the gap, but a window still exists
between a drifting merge and the audit run, so you may still be the first to see
it.

When a ratchet gate fails, do **not** reach for the fix that looks cheapest:
raising the ceiling (bumping `--max-warnings`) or widening a baseline so the
count matches again. That turns someone else's red green by loosening the very
gate that exists to stop the count from growing, and it hides the real
regression inside your unrelated change. Instead:

1. Confirm the failure is unrelated to your diff. The gate names the drifted
   count or file (a warning total, a census site, a baseline entry), so check
   whether your change could plausibly have moved it.
2. If it is inherited drift, do not absorb the fix into your PR. Check the Main
   Ratchet Audit tracking issue (or open a new issue) to see whether the drift
   is already known, and note it on your PR so a reviewer understands the red is
   pre-existing.
3. If you want to fix it, land the one-line ratchet correction as its own small,
   separate PR that credits the real cause (the merge that introduced the
   drift). Keep it out of the unrelated change so the history stays honest about
   what moved the number.

(For maintainers running babysit: its `known_reds`
(`src/kiro_crew/builtin_skills/kirocrew-dev/babysit/SKILL.md`) only tells one
operator's local watch loop to treat a known red as expected. It never makes a
required check pass, so it is not a substitute for any of the above.)

## Commit Messages

[Conventional Commits](https://www.conventionalcommits.org/):

```
<type>: <summary>

<body — what and why, not how>
```

Types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`

Rules: imperative mood, lowercase summary, no trailing period, wrap body at 72 chars.

## Recognizing Contributions

There is one contributor list, the Contributors block in [README.md](README.md).
Deliberately one: a second table for "other" contributions would rank one kind of
help above another, and split recognition across two places nobody reads twice.

Two things are credited automatically by a daily job: authoring a merged pull
request, and reporting an issue that a merged pull request closed. You do not need
to ask for either.

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
