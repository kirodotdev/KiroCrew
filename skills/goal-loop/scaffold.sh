#!/usr/bin/env bash
# scaffold.sh — goal-loop wrapper around self-nudge-loop/scaffold.sh
#
# Generates a goal-driven loop anchor tree:
#   <anchor>/GOAL.md        (this script)
#   <anchor>/LOOP.md        (via self-nudge-loop/scaffold.sh)
#   <anchor>/README.md      (via self-nudge-loop/scaffold.sh)
#   <anchor>/board/         (via self-nudge-loop/scaffold.sh)
#
# Usage:
#   scaffold.sh --project <name> --anchor-dir <abs> --goal "<goal>" [--force]
#
# User responsibilities after running:
#   1. Fill the 5 DoD criteria in <anchor>/LOOP.md
#   2. Review <anchor>/GOAL.md discovery sources
#   3. Arm via UI or REST (see LOOP.md §"Start the loop (REST)")
set -euo pipefail

PROJECT=""; ANCHOR=""; GOAL=""; FORCE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --project)    PROJECT="$2"; shift 2 ;;
    --anchor-dir) ANCHOR="$2"; shift 2 ;;
    --goal)       GOAL="$2"; shift 2 ;;
    --force)      FORCE=1; shift ;;
    -h|--help)    sed -n '2,18p' "$0"; exit 0 ;;
    *)            echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ -z "$PROJECT" || -z "$ANCHOR" || -z "$GOAL" ]] && {
  echo "missing --project, --anchor-dir, or --goal" >&2; exit 2; }
[[ "$ANCHOR" != /* ]] && { echo "--anchor-dir must be absolute" >&2; exit 2; }

# Delegate anchor-tree generation to self-nudge-loop (mechanics, LOOP.md, board).
UPSTREAM="$(dirname "$(readlink -f "$0")")/../self-nudge-loop/scaffold.sh"
[[ ! -x "$UPSTREAM" ]] && { echo "missing upstream: $UPSTREAM" >&2; exit 2; }

FORCE_FLAG=()
[[ "$FORCE" -eq 1 ]] && FORCE_FLAG=(--force)
"$UPSTREAM" --project "$PROJECT" --anchor-dir "$ANCHOR" --with-board ${FORCE_FLAG[@]+"${FORCE_FLAG[@]}"}

# Write the goal statement + discovery-source conventions.
GOAL_MD_PATH="$ANCHOR/GOAL.md"
if [[ -e "$GOAL_MD_PATH" && "$FORCE" -eq 0 ]]; then
  echo "skip (exists): $GOAL_MD_PATH"
else
  cat > "$GOAL_MD_PATH" <<EOF
# GOAL — $PROJECT

Generated: $(date -u +%FT%TZ) by \`goal-loop/scaffold.sh\`.

---

## The goal

> $GOAL

Loop retires when all Definition-of-Done criteria in \`LOOP.md\` are true.

---

## Issue-discovery sources

Every cycle, if the todo column is empty, the agent must run these and
\`kanban-md create\` a card for each **new** finding (skip findings already
on the board by title match):

- **Tree grep.** \`grep -rnE '(TODO|FIXME|XXX|TBD)' $ANCHOR\` scoped to the
  project tree. Obfuscate the markers in any card body you write (e.g.
  \`T\${""}ODO\`) so DoD-4 zero-markers stays satisfiable.
- **Failed tests** (if applicable). Last ToD / CI / pytest run in the
  project. Each failure = one card.
- **User-reported issues** in chat (if any are tagged with this project's
  name).

Add/remove sources to fit the goal. Keep this list short — each source adds
cycle cost.

## Cycle protocol

Re-read \`LOOP.md §"Operating invariants"\` every cycle. One cycle = one
atomic step. Never more than 5 tool calls per cycle.

Card lifecycle: **todo → in-progress → review**. Never move to done yourself
— human approves.

## Persistence rule (CRITICAL — re-read every cycle)

The loop stops in **exactly two cases**, nothing else:

1. **Goal achieved** — all DoD criteria in \`LOOP.md\` check green.
2. **Unrecoverable infrastructure error** — host/tooling itself broken
   (disk full, network down, auth provider out, kanban-md missing from
   PATH, kernel OOM). Log one-line diagnosis and stop.

Everything else is a problem to solve, not a stop condition:

- Test/build/lint failures → fix them, that's the job.
- "I don't know how" → read code, grep, log, probe, add a research card.
- A card is blocked → split it, unblock the dep, or pick another card.
- A tool erred → read the error, fix the invocation, retry.
- Tried twice, both failed → try a third angle; never stop on "tried twice".
- Cycle budget feels tight → the service enforces \`max_cycles\`, not you.

When in doubt: keep going, find another angle, create a new card, and tick.
Only write \`STOP\` or call \`autonudge_stop\` for the two cases above.

## Kill switches

- \`touch $ANCHOR/STOP\` — agent halts at next cycle entry.
- UI 🎯 → Stop loop — service stops scheduling.
- \`autonudge_stop(reason="...")\` — in-loop self-halt MCP tool.
EOF
  echo "wrote: $GOAL_MD_PATH"
fi

cat <<EOF

goal-loop anchor ready at: $ANCHOR
Next:
  1. Fill 5 DoD criteria in: $ANCHOR/LOOP.md
  2. Review discovery sources in: $GOAL_MD_PATH
  3. ls $ANCHOR/STOP  (must say "No such file")
  4. Arm the loop (UI 🎯 "Set a goal" or REST) — see $ANCHOR/LOOP.md §"Start the loop (REST)"
EOF
