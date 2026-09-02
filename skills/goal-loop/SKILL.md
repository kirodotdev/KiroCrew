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
└── board/         ← kanban-md board, 6 columns (from self-nudge-loop)
```

The loop agent re-reads `GOAL.md` + `LOOP.md` every cycle. That's the
"mission briefing" — everything else is session state.

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
every cycle (`kanban-md pick`, `create`, `move`, `handoff`). It is a
single-binary Go tool from
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

The loop needs the `pick`, `create`, `move`, and `handoff` verbs; check for those
rather than a version number.

If `kanban-md` is not on PATH, the scaffold still generates `board/` as a
plain markdown directory you can hand-edit, but the loop's auto-claim /
auto-move steps will fail and the agent will block every cycle. Install the
CLI before arming the loop for unattended runs.

**KiroCrew `autonudge_stop` MCP tool** — shipped with KiroCrew ≥ the
autonudge CR. Used by the agent to self-halt when DoD is met. No extra
install.

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
2. Open `<anchor>/GOAL.md` — confirm issue-discovery sources (defaults: tree
   grep, kanban backlog). Add/remove.
3. `ls <anchor>/STOP` must say "No such file".
4. Arm the loop: `monitor_start(message, interval_secs, max_cycles)` from a live
   session, the UI 🎯 "Set a goal", or `POST /api/autonudge`. Revise a running
   loop in place with `PATCH /api/autonudge/{loop_id}` (or `monitor_update`),
   which keeps its cycle count; `DELETE /api/autonudge/{loop_id}` stops it.
   Changing the goal TEXT over REST also needs `expect_fingerprint` (the
   `message_fingerprint` from a GET) or the call answers 409; `monitor_update`
   threads it for you.

## The goal-loop cycle (what the agent does)

The nudge written by this skill instructs the agent to, every cycle:

1. **STOP / DoD checks first** — if STOP exists or all DoD criteria met, call
   `autonudge_stop` and stop.
2. **Claim work** — `kanban-md pick` the next unblocked todo. If none, go to 3.
3. **Discover issues** — run the discovery sources from GOAL.md. For each
   finding not already on the board, `kanban-md create`. Then pick.
4. **Execute one atomic step** on the claimed card (≤5 tool calls).
5. **Record** — edit card body with `<UTC> cycle-<n>: <verb> <outcome>`, and call
   `session_ledger_record` with the phase, `next` as a concrete intent, and any
   approach tried and rejected. The ledger survives context compaction; a card's
   Cycle Log does not. On resume, read `session_ledger_read` before re-deriving
   state from the board. Move the card to Review when it is ready for human
   approval.
6. **DM the user** — one-line progress tick via `send_message`.

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

**The loop stops in exactly two cases, nothing else:**

1. **Goal achieved** — all DoD criteria in `LOOP.md` check green → call
   `autonudge_stop(reason="DoD met")`.
2. **Unrecoverable infrastructure error** — the host/tooling itself is
   broken in a way the agent cannot route around: disk full, network
   partition, auth provider down for >3 cycles, kanban-md binary missing
   from PATH mid-loop, kernel OOM, etc. Log one-line diagnosis to the
   Cycle Log and call `autonudge_stop(reason="infra: <what>")`.

**Everything else is a problem to solve, not a reason to halt.** Examples
of things that are NOT stop conditions:

- Test failures, build errors, lint errors → fix them, that's the job
- "I don't know how" → read code, grep, check logs, try a smaller probe,
  add a research card to the board, read a skill for context
- A card seems blocked → split it, unblock dependencies, or mark the
  blocker explicit and pick a different card
- A tool returned an error → read the error, correct the invocation, retry
- The goal feels unreachable → re-read GOAL.md, decompose into smaller
  cards, run discovery again
- You already tried X twice → try Y; if Y fails, try Z; never stop on
  "tried twice"
- It's late in the cycle budget → keep working; the service enforces
  `max_cycles`, not you

The only time the agent writes a STOP sentinel or calls `autonudge_stop`
itself is for the two cases above. When in doubt: keep going, find another
angle, create a new card, and tick. The loop exists precisely so the agent
can grind through problems humans would give up on.

## Extension points (future)

- `--seed-backlog <file>` — pre-populate the board from a newline-delimited
  list of card titles
- `--agent <name>` — write an `agent:` hint to GOAL.md so loops launched from
  chat pick up a specialist
- `--discovery <source>` — multi-select: grep, taskei, tod-failures

Out of scope for v1. Ship the tiny version, iterate when you hit a real
missing feature.
