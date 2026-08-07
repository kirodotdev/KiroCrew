# Auto-Improvement

Measures a GitHub repository before it changes it.

Most "AI improves your code" loops optimize whatever their metric happens to
report, which means a noisy or wrong metric produces confident nonsense. This app
inverts the order: it builds a metric, **proves** the metric can detect a known
win, and only then starts changing code. Survivors become draft pull requests you
review.

> The design thesis, kept from the original: *a measurement system that happens
> to write code, not a code-writing system that happens to measure.*

**Using it?** Start with the [user manual][manual]. This README is the
engineering overview; the manual is the operator's guide.

[manual]: docs/MANUAL.md

## How a run works

**Phase 1 — build and prove the ruler.** Calibrate a noise band from ~30
baseline repetitions (`band = max(2σ, floor)`), then run a **canary**: a known
win that must clear the band. If the canary fails, the harness cannot measure and
the run **halts** rather than optimizing noise. A host-pollution check also runs:
state is hashed before and after booting the measurement runtime, and any drift
blocks the run.

**Phase 2 — improve, one disposable cycle at a time.**

| Phase | What happens | Who decides |
|---|---|---|
| A · Discover | profile hot paths, or lint/inspect failure surfaces | agent |
| B · Propose | N candidates in parallel `git worktree`s | agent |
| C · Gate | edit-allowlist check, then build + tests | deterministic |
| D · Measure | serial pinned interleaved A/B, median not mean | deterministic |
| D' · Bug gate | RED×2 on base → GREEN on fix → whole suite stays green | deterministic |
| E · Keep or revert | accept only if the win clears the noise band | deterministic |
| F · Draft PR | second independent reproduce, then `gh pr create --draft` | deterministic |

Every phase that decides anything is deterministic Python. The agent proposes; it
never grades its own work.

## Safety model

The agent is treated as adversarial toward the metric, because a metric is easier
to game than a codebase is to improve.

- **Push-disabled clone.** The target clone's push remote is pinned to
  `DISABLED_NO_PUSH` and the driver refuses to start otherwise.
- **Draft-only output.** PRs are created with `--draft`. Nothing here publishes,
  marks ready for review, merges, or enables auto-merge.
- **Generated branches only.** A PR's head branch is
  `auto-improvement/<kind>-<fingerprint>` — never a branch you work on. It is
  checked against a non-overridable protected-branch denylist that a hand-edited
  config cannot widen.
- **Edit allowlist.** The ruler, the measurement harness, the tests-of-record,
  and auth paths are mechanically off-limits; a candidate touching them is
  rejected without being measured.
- **Reward-hack guards.** Checks the build/test gate structurally cannot see —
  no silent capability shrink, and a held-out functional probe must still pass.

## Chats

Each pull request, finding, ruler, and run gets its own **resumable** chat
session, filed into an `Auto-Improve - <repo>` folder. Clicking "discuss" twice
returns to the same conversation rather than starting a second one. The
autonomous loop's own agent runs happen on silent sessions, so a night's run does
not fill the chat surface with agent cards.

## Requirements

- `git` and an authenticated `gh` CLI on the gateway host
- A GitHub repository you can open pull requests against

## Layout

```
app.json            manifest (opt-in; defaultEnabled false)
backend/
  routes.py         in-process aiohttp routes under /api/apps/auto-improvement
  store.py          artifact + session-record layout under the app data dir
  pr_checks.py      PR status, CI checks, and the watcher verdict
spine/              the target-agnostic engine (driver, gate, measurer, keeper…)
profiles/
  github_repo/      the GitHub target profile (PR recipe lives here)
skills/             ai-discover, metric-design
agents/             discovery, pr-author
docs/MANUAL.md      user manual — how to run it, read findings, and configure it
docs/PORT_PLAN.md   why the port is shaped the way it is
```

The **spine** is deliberately target-agnostic: it consumes a target only through
the six-field `TargetProfile` seam (ruler, build gate, edit allowlist, isolation
recipe, PR recipe, calibration params). Adding a new target means adding a
profile, not touching the engine.

## Tests

```bash
python -m pytest src/kiro_crew/apps/builtins/auto_improvement/tests/ \
  --override-ini="addopts="
```

These are not in the default `testpaths`, so they need an explicit path.
