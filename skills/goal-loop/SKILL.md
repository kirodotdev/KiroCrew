---
name: goal-loop
description: Bootstrap a goal-driven self-improving AutoNudge loop from a goal plus an anchor directory, then run the board autonomously until the Definition of Done is met.
triggers: goal-driven loop, set up a goal loop, autonomous fix loop, run until done
---

# goal-loop

Thin orchestration skill on top of `self-nudge-loop`. One extra file
(`GOAL.md`) and one extra script (`scaffold.sh`) that calls the underlying
self-nudge-loop scaffold and appends goal-specific conventions.

## What you get

After running this skill's `scaffold.sh`, the anchor directory contains:

```
<anchor>/
├── GOAL.md        ← your goal statement + issue-discovery rules (this skill)
├── LOOP.md        ← DoD placeholder + REST arming recipe (from self-nudge-loop)
├── README.md      ← directory map (from self-nudge-loop)
└── board/         ← kanban-md board, 6 columns (only when kanban-md is installed)
```

The loop agent re-reads `GOAL.md` + `LOOP.md` every cycle — but only because
the nudge you arm tells it to (step 4 under "Run it"): LOOP.md's own nudge never
names `GOAL.md`. That's the "mission briefing" — everything else is session state.

## When to use

- User sets a concrete, verifiable goal ("get this test suite to 100% pass",
  "migrate all callers off module X", "drain DLQ Y").
- You want the agent to **discover issues itself** (grep TODO/FIXME, failed
  tests, open tickets) and convert them to board cards.
- You trust the agent to run for 10+ cycles autonomously without human
  per-step review.

## When NOT to use

- One-off fix → just use the agent directly
- Already-defined backlog → use `self-nudge-loop/scaffold.sh` directly
- Goal cannot be expressed as ≤5 shell-checkable DoD criteria → split the
  goal first

## Prerequisites

**`kanban-md` CLI** — required for the board operations the loop agent runs
every cycle (its `list`, `pick`, `create`, `move`, `edit` and `handoff`
subcommands). It is a single-binary Go tool from
[github.com/antopolskiy/kanban-md](https://github.com/antopolskiy/kanban-md).

Install (pick one):

```bash
# Homebrew (macOS/Linux) — recommended, auto-updates
brew install antopolskiy/tap/kanban-md

# Go (if you already have Go on PATH)
go install github.com/antopolskiy/kanban-md/cmd/kanban-md@latest

# Pre-built binary from GitHub Releases
#   https://github.com/antopolskiy/kanban-md/releases
#   download the linux/amd64 (or matching) tarball, extract to ~/.local/bin/
```

Verify:

```bash
command -v kanban-md
```

The loop's commands were written for and checked against kanban-md 0.37.0; the
cycle uses its `list`, `pick`, `create`, `move`, `edit` and `handoff` subcommands.
If a newer kanban-md rejects one with `unknown flag`, compare that subcommand's
`--help` output with the command; the `self-nudge-loop` skill's commands and its
test are where they are kept in step.

If `kanban-md` is not on PATH, the scaffold skips `board/` creation and
prints a warning. Install the CLI before arming a goal loop; this skill requires
the board for automatic claim and handoff.

The current scaffold also requires a `readlink` implementation that supports
`-f` (GNU coreutils). Stock macOS `readlink` does not provide that flag.

**Kiro Crew `autonudge_stop` MCP tool** — included with Kiro Crew. Used
by the agent to self-halt when DoD is met; no extra install is required.

## Run it

From a repo checkout (this skill and its `scaffold.sh` are repo-checkout-only, so
the path below exists only where `KIROCREW_PROJECT_DIR` names such a checkout):

```bash
cd ~/.kiro/crew/skills/goal-loop
./scaffold.sh \
  --project my-goal-name \
  --anchor-dir /abs/path/to/goal/anchor \
  --goal "Get MyService integration tests to green on AL2023"
```

Then:

1. Open `<anchor>/LOOP.md` — fill in 5 shell-checkable DoD criteria.
2. Open `<anchor>/GOAL.md` — replace `<PROJECT_TREE>` and confirm the
   issue-discovery sources (defaults: tree grep, failed tests, user-reported
   issues). Add/remove.
3. `ls <anchor>/STOP` must say "No such file".
4. Arm the loop with LOOP.md's "Ready-to-paste nudge", prefixed with the line
   `Every cycle, read <anchor>/GOAL.md first; its rules override this nudge where they differ.`
   (the scaffold prints it). Without that line the agent never reads GOAL.md and
   follows LOOP.md's nudge alone, including its "all blocked" exit. Arm via
   `monitor_start(message, interval_secs, max_cycles, max_runtime_secs)` from a live
   session, the UI 🎯 "Set a goal", or `POST /api/autonudge`. Revise a running
   loop in place with `PATCH /api/autonudge/{loop_id}` (or `monitor_update`),
   which keeps its cycle count; `DELETE /api/autonudge/{loop_id}` stops it.

## The goal-loop cycle (what the agent does)

LOOP.md's nudge plus GOAL.md (read first, per the armed prefix) instruct the
agent to, every cycle:

1. **STOP / DoD checks first** — if STOP exists or all DoD criteria met, call
   `autonudge_stop` and stop.
2. **Resume or claim work** — `kanban-md --dir <BOARD> list --claimed-by loop-<project> --json`;
   resume a held card if any (a blocked one: release it as described under
   "not stop conditions" below and pick instead), otherwise `kanban-md --dir <BOARD> pick --claim loop-<project> --status todo`
   to claim the next unblocked todo. Keep the same project claimant every cycle. If none, go to 3.
3. **Discover issues** — run the discovery sources from GOAL.md. For each
   finding not already on the board, `kanban-md create`. Then pick.
4. **Execute one atomic step** on the claimed card (≤5 tool calls).
5. **Record** — edit card body with `<UTC> cycle-<n>: <verb> <outcome>`, and call
   `session_ledger_record` with the phase, `next` as a concrete intent, and any
   approach tried and rejected. The ledger survives context compaction; a card's
   Cycle Log does not. On resume, read `session_ledger_read` before re-deriving
   state from the board. When the card is ready for human approval, move it to
   Review with `kanban-md --dir <BOARD> handoff <id> --claim loop-<project> --note "<summary>" --release`
   (`--release` drops the claim so the next cycle's `--claimed-by` scan does
   not resume a card already in Review).
6. **Notify on milestones only** — a phase boundary, blocker or completion, via
   `send_message`. With no `session` argument it is a dashboard notification;
   pass `session="slack"` (or another channel) for an owner DM. No per-cycle tick.

## Operating invariants

Inherited verbatim from `self-nudge-loop/LOOP.md §"Operating invariants"`.
See that file. The short list:

- If the armed nudge has gone stale, revise it with `monitor_update` rather than
  re-arming, which loses the cycle count

- Never `git push`
- Never read credential files as text
- Never hand-edit kanban .md files — use `kanban-md` CLI
- One cycle = one atomic step
- Stay silent in chat unless DoD met, hard blocker, or STOP tripped

## Persistence rule (CRITICAL)

**The agent self-stops in exactly three cases:**

1. **User stop** — the configured `STOP` sentinel exists → call
   `autonudge_stop(reason="sentinel")`.
2. **Goal achieved** — all DoD criteria in `LOOP.md` check green → call
   `autonudge_stop(reason="DoD met")`.
3. **Unrecoverable infrastructure error** — the host/tooling itself is
   broken in a way the agent cannot route around: disk full, network
   partition, auth provider down for >3 cycles, kanban-md binary missing
   from PATH mid-loop, kernel OOM, etc. Log one-line diagnosis to the
   Cycle Log and call `autonudge_stop(reason="infra: <what>")`.

The service may also deactivate the loop on its finite cycle/runtime bounds or
an approval stall. Those are bounded stops, not evidence that the goal succeeded.

**Everything else is a problem to solve, not a reason to halt.** Examples
of things that are NOT stop conditions:

- Test failures, build errors, lint errors → fix them, that's the job
- "I don't know how" → read code, grep, check logs, try a smaller probe,
  add a research card to the board, read a skill for context
- A card seems blocked → split it, unblock dependencies, or mark the
  blocker explicit with `kanban-md --dir <BOARD> edit <id> --block "<reason>" --release`
  (it drops the claim, so the `--claimed-by` resume scan stops returning the
  card) and pick a different card
- A tool returned an error → read the error, correct the invocation, retry
- The goal feels unreachable → re-read GOAL.md, decompose into smaller
  cards, run discovery again
- You already tried X twice → try Y; if Y fails, try Z; never stop on
  "tried twice"
- It's late in the cycle budget → keep working; the service enforces
  `max_cycles`, not you

The agent never creates the user's STOP sentinel itself. It calls
`autonudge_stop` only for the three cases above; service-enforced bounds remain
independent. When in doubt: keep going, find another angle, create a new card,
and tick.

## Extension points (future)

- `--seed-backlog <file>` — pre-populate the board from a newline-delimited
  list of card titles
- `--agent <name>` — write an `agent:` hint to GOAL.md so loops launched from
  chat pick up a specialist
- `--discovery <source>` — multi-select: grep, taskei, tod-failures

Out of scope for v1. Ship the tiny version, iterate when you hit a real
missing feature.
