---
name: kirocrew-worktree-dev
description: "HARD RULE for local KiroCrew feature development: every change is built and verified inside a git worktree, never against the live gateway. Covers worktree creation, the build gate (pytest + flake8 + tsc + vitest), the built-dist gotcha, feature flags in shared config, and multiple paths for previewing a worktree live (pod, dev-backend.sh, or build+PR only). Use whenever building, testing, switching, or verifying a KiroCrew feature locally."
triggers: worktree, feature branch, new feature, local dev, develop feature, create worktree, git worktree
---

# HARD RULE: KiroCrew feature development happens inside a git worktree

Every local KiroCrew change — frontend, backend, or both — is developed, built,
and verified in a dedicated **git worktree**, never by editing the live checkout
or developing against the running gateway directly. One feature = one worktree.
This is the single most important rule; violating it (Rule 1) is the most common
way to waste hours.

## Rule 0 — Every change is developed in a worktree (FE + BE together)

- **Every** KiroCrew change happens in a dedicated worktree. Never edit the
  live/production checkout, and never develop against the running gateway.
- A KiroCrew feature spans **two layers**: `src/kiro_crew/` (backend, Python)
  and `website/` (frontend, React/Vite). A worktree carries both; even a
  backend-only change lives in a worktree.
- **Single-active model.** Making a worktree "live" swaps the *code* behind the
  same dashboard URL and the same shared `~/.kirocrew` home/DB/sessions — you
  are on your REAL data. Only one worktree is live at a time. Be deliberate
  about migrations, and switch back to the clean baseline when done.

## Rule 1 — Create a worktree off `main`

```bash
# From your main KiroCrew clone:
git worktree add ../kirocrew-wt-<name> -b feat/<name> origin/main
cd ../kirocrew-wt-<name>
```

This gives you an isolated directory with its own branch. All work happens
inside this worktree directory — never in the main clone.

To list existing worktrees:
```bash
git worktree list
```

To clean up after merging:
```bash
git worktree remove ../kirocrew-wt-<name>
```

## Rule 2 — The build gate (ALL must pass before PR)

Run these from the worktree root:

```bash
# Backend (Python)
python -m pytest --override-ini=addopts= -q
flake8 src/ test/

# Frontend (TypeScript + React)
cd website
npx tsc -b
npx vitest run
cd ..
```

**All four gates must be green.** 0 test failures required. Never weaken or skip
tests to go green. `isort --check-only src/ test/` is recommended but not
blocking.

**Order matters:** if you changed frontend code, rebuild the dist (Rule 3)
before running backend tests that import static assets.

## Rule 3 — The served frontend is a built `dist`, not a dev server

- The gateway serves the frontend from `src/kiro_crew/static/dist/` (a compiled
  bundle). Source `.tsx` edits are invisible until the website is rebuilt.
- After frontend changes:
  ```bash
  cd website && npm ci && npm run build && cd ..
  ```
  This places the built SPA into `src/kiro_crew/static/dist/`.
- `dist/` is gitignored → it does NOT transfer via `git fetch` or worktree
  creation. After creating a worktree or any frontend change you MUST rebuild.
- Component names are minified in the production bundle — when checking whether
  a feature compiled in, grep for surviving string literals (route paths,
  `/api/...`, visible labels), not React component names.

## Rule 4 — Feature flags live in shared `~/.kirocrew/config.json`

- All worktrees share `~/.kirocrew` (single-active model). Feature flags belong
  in shared config, not per-worktree code, so they persist across switches.
  Config is read live (fingerprint cache) — editing the file is picked up
  without a gateway restart.
- If a flagged feature "doesn't show," check the flag in shared config BEFORE
  suspecting the bundle — an absent flag, not a missing build, is the common
  cause.

## Rule 5 — Previewing a worktree live: multiple paths

**Build gates green is the floor** — it proves the code compiles and tests pass.
Actually *running* the worktree to click through it is an **optional** preview
step with several paths; use whichever your environment supports:

1. **`dev-backend.sh` (simplest).** From the worktree root:
   ```bash
   ./dev-backend.sh
   ```
   This starts the gateway on port 6777 using `.kirocrew-dev/` as its data
   directory (isolated from your production `~/.kirocrew/`). It uses
   `PYTHONPATH=src` so code changes are picked up on restart. Ctrl+C to stop,
   re-run after changes.

2. **Isolated pod (no cutover, hands-off).** Preview the full stack on its own
   port without touching the live gateway: `kirocrew pod up <worktree>` gives
   an isolated instance with its own `KIROCREW_HOME`. Best for QA/agents. See
   the bundled **`pod-e2e`** skill.

3. **No preview at all (also valid).** For many changes, the build gate + unit
   tests are enough confidence to cut the PR. Previewing live is optional.

## Rule 6 — Hands off the live plane

- Never edit the live/production checkout, and never start/stop the live gateway
  directly from a feature session. If you need to verify live, use
  `dev-backend.sh` (isolated port) or pods.

## Rule 7 — Submit a PR via GitHub

When the build gate is green and you're satisfied with the change:

```bash
git add -A
git commit -m "feat: <description>"
git push origin feat/<name>
gh pr create --base main --title "feat: <description>" --body "<details>"
```

- PRs target `main`.
- CI runs the same gates (pytest, flake8, tsc, vitest) — but run them locally
  first. CI is for confirmation, not discovery.
- Address review comments in the worktree, amend or add commits, force-push the
  branch.
- **QA media (screenshots / demo videos): review-then-attach.** Deliver the
  media to the user for review first; once the user approves it, attach it to
  the PR automatically without asking again — commit under
  `temp-screenshots/<feature>/`, amend into the single commit, force-push, and
  embed commit-SHA-pinned raw URLs in the PR body (images inline, mp4 as a
  labelled link). Full recipe: the **pod-e2e** skill → "Attach approved QA
  media to the PR".

## Why these rules exist (gotchas they prevent)

- Editing the live checkout → the running gateway picks up partial changes →
  runtime crashes or stale frontend served alongside new backend routes.
- `dist/` not rebuilt after a frontend change → the served frontend lacks the
  new feature even though the source has it (Rule 3).
- A flagged feature "missing" is usually an absent flag in **shared** config,
  not a missing bundle (Rule 4).
- Running tests against the main clone while developing in a worktree → you're
  testing the wrong code.
- Pushing directly to `main` → breaks CI for everyone; always use a feature
  branch + PR.
