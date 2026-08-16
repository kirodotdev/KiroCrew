---
name: kirocrew-worktree-dev
description: "HARD RULE for developing the Kiro Crew source repo ITSELF (not for users' own projects): every change is built and verified inside a git worktree, never against the live gateway. Covers worktree creation, the blocking local build gates (pytest + isort + flake8 + mypy + tsc + vitest), the built-dist gotcha, feature flags, live preview paths (dev-backend.sh or isolated pods), and the PR workflow. Use only when building, testing, switching, or verifying a change to Kiro Crew's own codebase."
triggers: kirocrew worktree, kirocrew build gate, kirocrew dev, kirocrew source, contribute to kirocrew, kirocrew repo
repo_scope: src/kiro_crew
---

# HARD RULE: Kiro Crew development happens inside a git worktree

> **Scope guard: this skill applies ONLY when the working directory is the
> Kiro Crew source repository (or a worktree of it).** If you are working in any
> other project, ignore this skill entirely — its rules (worktree mandate,
> build gates, single-commit squash, force-push) are conventions of the Kiro Crew repo
> and may be wrong or harmful elsewhere.

Every local Kiro Crew change — frontend, backend, or both — is developed, built,
and verified in a dedicated **git worktree**, never by editing the live checkout
or developing against the running gateway directly. One feature = one worktree.
This is the single most important rule; violating it is the most common way to
waste hours.

## Rule 0 — Every change is developed in a worktree (FE + BE together)

- **Every** Kiro Crew change happens in a dedicated worktree. Never edit the
  live/production checkout, and never develop against the running gateway.
- A Kiro Crew feature spans **two layers**: `src/kiro_crew/` (backend, Python)
  and `website/` (frontend, React/Vite). A worktree carries both; even a
  backend-only change lives in a worktree.
- **Single-active model.** Making a worktree "live" swaps the *code* behind the
  same dashboard URL and the same shared data home — `~/.kiro/crew` by default
  (legacy installs auto-migrate from `~/.kirocrew` on first launch;
  `KIROCREW_HOME` overrides) — including your REAL DB and sessions. Only one
  worktree is live at a time. Be deliberate about migrations, and switch back
  to the clean baseline when done.

## Rule 1 — Create a worktree off `main`

```bash
# From your main KiroCrew clone:
git fetch origin main
git worktree add ../kirocrew-wt-<name> -b feat/<name> origin/main
cd ../kirocrew-wt-<name>
```

This gives you an isolated directory with its own branch. All work happens
inside this worktree directory — never in the main clone.

Set up the worktree's own environment once (same dep set as CI — `dev` is a
PEP 735 dependency group, NOT a package extra, so it needs `--group`, which
requires pip ≥ 25.1: run `.venv/bin/pip install -U pip` first if it errors; on
Windows the venv binaries live under `.venv\Scripts\` instead of `.venv/bin/`):
```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[voice]" --group dev
cd website && npm ci && cd ..
```

To list existing worktrees: `git worktree list`
To clean up after merging: `git worktree remove ../kirocrew-wt-<name>`

## Rule 2 — The build gate (ALL must pass before PR)

**`.github/workflows/ci.yml` is the canonical gate list** — it is what actually
gates the PR; if this skill and CI disagree, CI wins. What this rule adds is
the worktree-specific gotchas (parallelism, mypy CI-parity, dist ordering),
not a replacement gate list.

Run from the worktree root:

```bash
# Backend (Python) — isort, flake8, mypy are ALL blocking in CI
python -m pytest -q          # setup.cfg addopts already runs xdist (-n auto --dist loadgroup)
isort --check-only src/kiro_crew test
flake8 src/kiro_crew test
mypy src/kiro_crew/

# Frontend (TypeScript + React)
cd website
npx tsc -b
npx vitest run
cd ..
```

**All gates must be green.** Never weaken or skip tests to go green.

**Run pytest in parallel — and keep `--dist loadgroup`.** The full backend
suite is large (16k+ tests); serial runs can exceed 45 minutes and time out in
agent sessions. The default `setup.cfg` addopts already includes `-n auto
--dist loadgroup` — `loadgroup` is required so `@pytest.mark.xdist_group`
serialization is honored; dropping it races those tests and produces flaky
failures. If you need to override addopts (e.g. to skip coverage during
iteration), use exactly this form, which preserves the xdist flags:

```bash
python -m pytest -q --override-ini="addopts=--ignore=build/private -n auto --dist loadgroup"
```

Never a bare `--override-ini=addopts=` (it silently drops `--dist loadgroup`).
For the full gate list itself, don't trust any restated copy (including this
one) — read `.github/workflows/ci.yml`, which is what actually gates the PR.

**Triaging failures: blame your change last, but verify.** Some hosts carry
environment-specific failures (permissions, missing optional binaries) that are
unrelated to any change. If a test fails, re-run it on a clean `origin/main`
checkout — if it fails there too, it's pre-existing and can be noted rather
than fixed; if it only fails on your branch, it's yours. Never label a failure
"known flaky" without that main-vs-branch comparison.

**A confirmed flake is a bug with a root cause, not noise to retry.** Do NOT add a
rerun, lengthen a `sleep`, or relax an assertion. Read
`docs/system-specs/common/testing-conventions.md` § Determinism for the four classes
and the one correct fix for each: seed nondeterministic input, poll instead of
sleeping, `MagicMock` for sync methods, `await` after `cancel()`. To find what is
actually flaky rather than guessing, mine CI instead of the local suite (a real flake
often will not reproduce on macOS at all):

```bash
gh run list --workflow=ci.yml --limit 250 --json databaseId,conclusion \
  --jq '.[]|select(.conclusion=="failure")|.databaseId' > /tmp/ids
xargs -P 10 -I{} sh -c 'gh run view {} --log-failed 2>/dev/null \
  | sed "s/\x1b\[[0-9;]*m//g" | grep -oE "_{3,} ?[A-Za-z_][A-Za-z0-9_.]* ?_{3,}"' \
  < /tmp/ids | sort | uniq -c | sort -rn | head -30
```

Rank by that frequency, and check each candidate against `origin/main` before fixing:
a ratchet/contract test failing on feature branches is a TRUE POSITIVE, not a flake.
The Windows shards fail far more than Linux, so expect timer-granularity and
process-semantics causes there.

**Suite speed: profile, don't guess.** At ~26.5k tests, per-test setup cost dominates
any single slow test. Time one file with `pytest test/test_x.py -n0 -q --no-cov
--durations=10` and compare a candidate fix **back to back** on the same machine
(`git stash`, run, pop, run), because a loaded host makes an absolute number
meaningless.
The recurring wins are an autouse fixture requesting an unused `tmp_path`, a real
`git` repo rebuilt per test instead of `copytree`d from a session template, and a
production poll the test never asserts on. After any speedup, mutate the covered
production code and confirm the test still fails. Full method and measured numbers:
`docs/system-specs/common/testing-conventions.md` § Keeping the suite fast.

**mypy must reproduce CI, not just "run".** CI installs `-e ".[voice]" --group
dev` (mypy pinned in `pyproject.toml` `[dependency-groups] dev`) and does NOT
install `faiss`. Two things make a local run diverge from CI:
- **Version drift** — error codes change between mypy versions; verify your
  `mypy --version` matches the pin in `pyproject.toml`.
- **Extra deps that CI lacks** — a dev venv with `faiss-cpu` installed gives
  `faiss.*` real types and makes mypy STRICTER than CI (false failures that CI,
  seeing `faiss` as a missing import, treats as `Any`). Do NOT use a
  faiss-equipped venv as a CI mirror.

Build a dedicated CI-parity venv once and reuse it for every worktree
(mypy reads config from the worktree root's `pyproject.toml`):

```bash
python3 -m venv ~/.kiro/crew/venvs/mypy-ci
~/.kiro/crew/venvs/mypy-ci/bin/pip install -e ".[voice]" --group dev   # from repo root; never add faiss
# then from any worktree root:
~/.kiro/crew/venvs/mypy-ci/bin/mypy src/kiro_crew/
```

**Order matters:** if you changed frontend code, rebuild the dist (Rule 3)
before running backend tests that import static assets.

## Rule 3 — The served frontend is a built `dist`, not a dev server

- The gateway serves the frontend from `src/kiro_crew/static/dist/` (a compiled
  bundle). Source `.tsx` edits are invisible until the website is rebuilt.
- After frontend changes, build AND stage (the build outputs to `website/dist`;
  the gateway serves the staged copy — the copy step is NOT automatic, and the
  destination must be CLEARED first: Vite emits content-hashed filenames, so
  copying over an existing bundle accumulates stale assets that can be served
  or packaged):
  ```bash
  cd website && npm ci && npm run build && cd ..
  rm -rf src/kiro_crew/static/dist && cp -R website/dist src/kiro_crew/static/dist
  ```
  Or use `make build`, which does the frontend build + clean dist staging +
  install in one step.
- `dist/` is gitignored → it does NOT transfer via `git fetch` or worktree
  creation. After creating a worktree or any frontend change you MUST rebuild.
- Component names are minified in the production bundle — when checking whether
  a feature compiled in, grep for surviving string literals (route paths,
  `/api/...`, visible labels), not React component names.

## Rule 4 — Feature flags live in the active instance's `$KIROCREW_HOME/config.json`

- Flags belong in the config of the instance you are actually looking at.
  Each runtime has its own home: the live gateway uses `~/.kiro/crew/` (the
  default since the data-home move; legacy `~/.kirocrew` auto-migrates),
  `dev-backend.sh` uses the worktree's `.kirocrew-dev/`, and each pod has its
  own isolated `KIROCREW_HOME`. Editing `~/.kiro/crew/config.json` while
  previewing via dev-backend or a pod changes your PRODUCTION config and does
  nothing to the preview — edit the preview instance's own `config.json`.
- Config is read live (fingerprint cache) — edits are picked up without a
  gateway restart.
- Flags belong in config, not per-worktree code, so they persist across
  worktree switches when a worktree is made live (worktrees made live share
  the live `~/.kiro/crew` home).
- If a flagged feature "doesn't show," check the flag in the **running
  instance's** config BEFORE suspecting the bundle — an absent flag (or a flag
  set in the wrong instance's home), not a missing build, is the common cause.

## Rule 5 — Previewing a worktree live: multiple paths

**Build gates green is the floor** — it proves the code compiles and tests pass.
Actually *running* the worktree to click through it is an **optional** preview
step with several paths; use whichever your environment supports:

1. **`dev-backend.sh` (simplest).** From the worktree root:
   ```bash
   ./dev-backend.sh
   ```
   Starts the gateway on its own dev port using `.kirocrew-dev/` as its data
   directory (isolated from your production `~/.kiro/crew/`). It uses
   `PYTHONPATH=src` so code changes are picked up on restart. Ctrl+C to stop,
   re-run after changes.

2. **Isolated pod (no cutover, hands-off).** Preview the full stack on its own
   port without touching the live gateway:
   ```bash
   kirocrew pod up <worktree-name> --json   # own KIROCREW_HOME, own port, no crons
   kirocrew pod down <worktree-name>        # zero residue
   ```
   Best for QA agents and end-to-end tests. `kirocrew pod --help` for all verbs
   (`ls`, `status`, `logs`, `provision`, …). The worktree must be built first
   (venv + dist); `kirocrew pod up --provision` does the full on-ramp.

3. **No preview at all (also valid).** For many changes, the build gate + unit
   tests are enough confidence to cut the PR. Previewing live is optional.

### Agent specs + MCP servers are a SEPARATE isolation axis from the data home

`KIROCREW_HOME` isolates config, DB, sessions and workspace. It does **not**
isolate `~/.kiro/agents/*.json` — the specs that define which MCP servers exist.
That directory is machine-wide, and a gateway rewrites its specs on every start.

A worktree gateway is therefore **prevented from clobbering them**: you will see

```
Refusing to rewrite the shared agent home /home/<you>/.kiro/agents from the
git worktree at /workplace/<you>/kirocrew-wt-<name>: ...
```

That warning is the guard working, not a failure. Consequence to know about: the
preview runs against the **real install's** agents and MCP servers, so it is safe
but not self-contained — a change to Kiro Crew's own managed MCP servers
(`mcp-core`, `mcp-cron`, `mcp-computer`) is not exercised by a worktree preview.
Verify those with unit tests, or temporarily point the real spec at the worktree
and put it back afterwards.

**Do not reach for `KIRO_HOME` to get around this yet.** It is kiro-cli's
directory-wide override — it moves sessions, settings, skills and steering too,
and Kiro Crew still reads the host paths for most of those, so setting it breaks
session resume. Making it a real isolation switch means routing the remaining
~two dozen `~/.kiro/**` readers through `kiro_home()` first.

## Rule 6 — Hands off the live plane

- Never edit the live/production checkout, and never start/stop the live gateway
  directly from a feature session. If you need to verify live, use
  `dev-backend.sh` (isolated port) or pods.

## Rule 7 — Submit a PR via GitHub (only when the user asks)

**Committing, pushing, and opening a PR each require explicit user
authorization** — per repo convention (AGENTS.md), never `git commit` or
`git push` proactively, and pushing needs its own approval separate from
committing. A green build gate means the change is *ready* to publish, not
that you should publish it.

When the user asks for a PR:

```bash
git add <specific files>     # stage only the files this change touches, never blanket -A
git commit -m "feat: <description>"
git push origin feat/<name>
gh pr create --base main --title "feat: <description>" --body "<details>"
```

- PRs target `main`.
- **One commit per PR.** CI enforces single-commit hygiene. Squash before
  opening (`git rebase -i origin/main` or `git reset --soft origin/main` +
  one commit), and fold review-round fixes in with `git commit --amend` +
  `git push --force-with-lease origin feat/<name>` rather than stacking commits.
- **Push as a standalone command.** Prefer running `git push` (including
  `--force-with-lease`) on its own line with explicit remote and branch —
  some agent security policies fail closed on pushes embedded in compound
  commands (`&&`, `;`, pipes). Treating commit, push, and PR edits as three
  separate steps is safe everywhere.
- **UI screenshots:** embed commit-SHA-pinned same-origin URLs —
  `https://github.com/<owner>/<repo>/raw/<sha>/<path>` — never
  `raw.githubusercontent.com` (blocked by GitHub Camo on private repos).
  Re-pin the SHA after every squash or force-push.
- CI runs the same gates (pytest, isort, flake8, mypy, tsc, vitest) — but run
  them locally first. CI is for confirmation, not discovery.

## Rule 8 — Opening the PR is not the end: monitor it

Don't open a PR and walk away. Review bots and CI produce findings that need
triage, and the base branch moves under you. Pushes during the loop follow the
same rule as Rule 7: AGENTS.md requires explicit approval for every push, so a
monitoring loop's scope — including whether its fix-and-push rounds are
pre-approved — must be stated by the user when they ask for it (e.g. "babysit
this PR and push fixes"). Absent that, confirm before each push.

- From an interactive session, start a same-session monitoring loop (see the
  **babysit** skill / `monitor_start`): poll CI + review comments, fix
  legitimate findings in the worktree, amend + force-push, repeat until green.
- Read the **full bodies** of review-bot comments even when their checks show
  as passing — non-blocking MEDIUMs and advisory notes are still legitimate
  feedback.
- The **prepare-pr** skill covers the full commit→green loop end-to-end.
- Rebase when the base moves; re-verify gates after every rebase.
- Never merge on the user's behalf — merge-ready means green + approved +
  current, then hand it back.

## Rule 9 — Leave the worktree clean (so prune can reap it)

A worktree's tree MUST be clean when you finish a work session on it —
`git status --porcelain` should print **nothing**. This is not cosmetic: Dev
Fleet's **"Prune merged"** fail-closes on a dirty tree (a `merged_dirty`
verdict — untracked files count as dirty), so a merged worktree with even one
stray untracked file or one unrestored tracked file is **refused for deletion**
and piles up, wasting disk.

Two habits keep the tree clean:

- **Write scratch OUTSIDE the worktree.** PR bodies, build/test log dumps, QA
  artifacts, and temp notes must never be written into the worktree root. Use a
  temp dir instead:
  ```bash
  SCRATCH=$(mktemp -d)                          # e.g. /tmp/tmp.XXXXXX
  gh pr create --body-file "$SCRATCH/pr-body.md" ...
  python -m pytest -q > "$SCRATCH/fullsuite.log" 2>&1
  ```
  The lone exception is a **committed** deliverable — e.g. approved QA media
  under `temp-screenshots/<feature>/`, which is staged into the PR's commit and
  is therefore clean, not litter (see the pod-e2e skill).
- **Restore regenerated tracked files; delete stray untracked ones — but only
  what YOU produced.** Before you end the session, run `git status --porcelain`
  and INSPECT each line before touching it. Only discard content this session
  itself created or regenerated:
  ```bash
  git status --porcelain                        # MUST end up empty
  git diff config-baseline.json                 # inspect FIRST — confirm the
  git checkout -- config-baseline.json          # rewrite came from YOUR test run
  rm -f .pr-body.md                             # scratch YOU wrote this session
  ```
  `git checkout --` and `rm` are destructive and unrecoverable. If a modified
  tracked file or an untracked path was NOT produced by this session — or you
  can't tell — do NOT discard it: it may be the user's in-progress work. Ask
  the user (or leave it and report the dirty state) instead.

If you genuinely need a new ignored scratch pattern, add it to the root
`.gitignore` (the agent-workflow scratch section) rather than leaving it to
dirty every tree.

## Rule 10 — Comment style: explain why, don't restate code

Comments and docstrings describe **behavior and rationale** — the non-obvious
*why*, invariants, edge cases, units, security constraints. They are NOT a task
log and NOT a paraphrase of the code.

- **No process/task-log citations** in code: PR/CR numbers, review-round or
  finding markers (`GPT review round 4`, `R16 F3`, `Codex HIGH`), incident
  dates, milestone tags (`M1`, `v0.6.0`), commit SHAs. That history lives in git.
- **No historical narration** ("previously…", "used to…", "we now…"). State the
  CURRENT behavior in present tense; keep the reason if it explains the code's shape.
- **Don't restate the code.** Cut comments that only repeat the adjacent line
  (`i += 1  # increment i`, `# return result`). Keep it concise: if a comment
  adds nothing a reader wouldn't see at a glance, drop it — but keep any
  non-obvious *why*.
- **Exempt:** the `_vendor/` tree and semantic pragmas (`# type: ignore`,
  `# noqa`, `# pragma`, `// @ts-…`, `// eslint-…`) — never touch them.

Applies to any code you write or edit here, and to doc prose too: describe what
the system does, not a changelog of how it got there.

## Why these rules exist (gotchas they prevent)

- Editing the live checkout → the running gateway picks up partial changes →
  runtime crashes or stale frontend served alongside new backend routes.
- `dist/` not rebuilt after a frontend change → the served frontend lacks the
  new feature even though the source has it (Rule 3).
- A flagged feature "missing" is usually a flag set in the wrong instance's
  `KIROCREW_HOME`, not a missing bundle (Rule 4).
- Running tests against the main clone while developing in a worktree → you're
  testing the wrong code.
- Serial pytest on the full suite → 45-minute timeouts in agent sessions;
  bare `--override-ini=addopts=` drops `--dist loadgroup` → flaky races in
  `xdist_group`-serialized tests (Rule 2).
- A faiss-equipped or version-drifted mypy venv → local results that contradict
  CI in both directions (Rule 2: CI-parity venv).
- Multi-commit branches → PR Hygiene check fails; squash first (Rule 7).
- Committing or pushing without an explicit user request → violates the repo's
  agent safety convention (Rule 7).
- Pushing directly to `main` → breaks CI for everyone; always use a feature
  branch + PR.
- Scratch files (PR bodies, log dumps) written into the worktree, or a tracked
  baseline a test rewrote and never restored → `git status` is dirty → Dev
  Fleet "Prune merged" fail-closes (`merged_dirty`) and merged worktrees pile
  up, wasting disk (Rule 9).
