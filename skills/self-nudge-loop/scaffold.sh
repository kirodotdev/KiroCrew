#!/usr/bin/env bash
# scaffold.sh — generate a hardened AutoNudge loop anchor tree for a project.
#
# Usage:
#   scaffold.sh --project <name> --anchor-dir <abs path> [--with-board] [--force]
#
# Produces (inside <anchor-dir>):
#   LOOP.md       — hardened template with DoD placeholder, safe nudge, REST table
#   README.md     — directory map + non-negotiables
#   board/        — kanban-md board (if --with-board and `kanban-md` on PATH)
#
# Idempotent: refuses to overwrite existing files unless --force.
# Does NOT run git. Does NOT arm the loop. Read the generated LOOP.md, fill in
# the DoD criteria and invariants, then arm via UI or REST.
set -euo pipefail

PROJECT=""; ANCHOR=""; WITH_BOARD=0; FORCE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --project)    PROJECT="$2"; shift 2 ;;
    --anchor-dir) ANCHOR="$2"; shift 2 ;;
    --with-board) WITH_BOARD=1; shift ;;
    --force)      FORCE=1; shift ;;
    -h|--help)    sed -n '2,15p' "$0"; exit 0 ;;
    *)            echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ -z "$PROJECT" || -z "$ANCHOR" ]] && { echo "missing --project or --anchor-dir" >&2; exit 2; }
[[ "$ANCHOR" != /* ]] && { echo "--anchor-dir must be absolute" >&2; exit 2; }

mkdir -p "$ANCHOR"
STOP_PATH="$ANCHOR/STOP"
BOARD_PATH="$ANCHOR/board"

write_if_absent() {  # $1=path  $2=content
  if [[ -e "$1" && "$FORCE" -eq 0 ]]; then
    echo "skip (exists): $1"
  else
    printf '%s' "$2" > "$1"
    echo "wrote: $1"
  fi
}

LOOP_MD=$(cat <<EOF
# LOOP — $PROJECT continuous session guide

One page. Tells you how to launch an autonomous AutoNudge loop in this KiroCrew session for **$PROJECT**.

Generated: $(date -u +%FT%TZ) by \`self-nudge-loop/scaffold.sh\`.

---

## Definition of Done

The loop retires itself when **all** are true:

1. <TODO: boolean criterion 1 — shell-checkable if possible>
2. <TODO: boolean criterion 2>
3. <TODO: boolean criterion 3>
4. **Zero stale markers.** \`grep -rn "TODO\\|FIXME\\|XXX\\|TBD" $ANCHOR\` returns empty.
5. **Board drained** (if using kanban-md): no cards in todo/in-progress/review.

When all five are true, agent posts the DoD checklist with ticks, calls \`autonudge_stop(reason="DoD met")\`, stays silent.

---

## Start the loop (UI)

1. \`ls $STOP_PATH\` → must say "No such file".
2. Click the 🎯 "Set a goal" (bullseye) icon in the composer toolbar.
3. Popover — **all four fields** (no blanks):
   - Nudge: paste from §"Ready-to-paste nudge" below
   - Idle seconds: \`60\`
   - Max cycles: \`30\`
   - Stop sentinel path: \`$STOP_PATH\`
4. **Start loop**.

## Start the loop (REST)

\`\`\`bash
# 1. Get a local user token (loopback only; requires X-Local-Secret header).
SECRET=\$(cat ~/.kirocrew/.local_secret)
TOKEN=\$(curl -sf -H "X-Local-Secret: \$SECRET" \\
  "http://127.0.0.1:5476/api/token/local?ttl=1h" \\
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"])')

# 2. Arm the loop — ?token= query param (NOT Authorization: Bearer).
curl -X POST "http://127.0.0.1:5476/api/autonudge?token=\$TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{
    "slot_key": "<slot>",
    "message": "<paste nudge>",
    "idle_secs": 60,
    "max_cycles": 30,
    "stop_sentinel_path": "$STOP_PATH"
  }'
\`\`\`

| Method | Path | Purpose |
|---|---|---|
| GET | \`/api/autonudge\` | list all loops |
| GET | \`/api/autonudge/slot/{slot_key}\` | loop bound to a slot |
| POST | \`/api/autonudge\` | start / replace a loop |
| PATCH | \`/api/autonudge/{loop_id}\` | edit nudge / idle / active |
| DELETE | \`/api/autonudge/{loop_id}\` | stop + remove |

---

## Ready-to-paste nudge

\`\`\`
Continue the $PROJECT loop.
Definition of Done: $ANCHOR/LOOP.md §Definition of Done.

STOP / EXIT CHECKS (every cycle, in order, before anything else):
1. If $STOP_PATH exists: call autonudge_stop(reason="sentinel"), post "Loop halted by sentinel.", do nothing else.
2. If the Definition-of-Done criteria in LOOP.md are all met: call autonudge_stop(reason="DoD met"), post the DoD checklist with ticks, stop.

BOARD (skip this block if no kanban-md board in use):
   BOARD=$BOARD_PATH
3. kanban-md --dir \$BOARD list --status todo --json
4. kanban-md --dir \$BOARD pick --assignee loop-<cycle_n>
5. If pick returns nothing and a dep-met backlog card exists: kanban-md --dir \$BOARD move <id> --status todo ; then pick.
6. If everything blocked AND you already posted a blocker this arming: autonudge_stop(reason="all blocked"), stop.

EXECUTE (<=5 tool calls per cycle, hard cap):
7. Read the one spec file for the claimed task.
8. Do ONE atomic thing: draft a fix, write a test, run a test, invoke one SOP. Never all at once.
9. NEVER git push. NEVER destructive ops. NEVER reply to real production tickets — sandbox / read-only only.
10. Cookie-jar auth: http.cookiejar.MozillaCookieJar(path).load() + urllib opener, OR curl -b <cookie-jar> -f -s. NEVER read a credential/cookie file as text. NEVER echo cookie contents in any error — scrub exceptions to type(e).__name__.

RECORD:
11. Append progress to the claimed card (kanban-md edit --add-body) or to a Cycle Log section in LOOP.md.
12. DM the owner a tick via send_message (owner DM is default — no channel arg). Template:
    "🎯 $PROJECT cycle-<n> · <task-id>  Done: <1-line>  Next: <1-line>  Status: <col>"
    One DM per productive cycle. Skip halted / no-op cycles.
13. If task complete: handoff to Review (NOT Done — human approves Done).

STAY SILENT in the chat panel unless: DoD met, hard blocker needs user decision, or STOP sentinel tripped.
(send_message DMs are expected every productive cycle — that is the progress channel, not the chat.)
One cycle = one step.
\`\`\`

---

## Kill switches

| Method | Speed | Notes |
|---|---|---|
| Agent self-halt via \`autonudge_stop\` | next cycle | Cleanest. Works unattended. |
| UI 🎯 → Stop loop | instant | Fastest manual. |
| \`touch $STOP_PATH\` | <=60s | Agent halts and calls autonudge_stop; service honors sentinel. |
| \`max_cycles: 30\` reached | bounded | Loop deactivates (not removed). Re-arm via PATCH/UI. |
| REST DELETE \`/api/autonudge/{loop_id}\` | instant | Clean removal. |

---

## Operating invariants (never violate)

1. Never \`git push\`. Humans push.
2. Never run destructive ops.
3. Never read credential files as text (\`~/.aws/*\`, \`~/.ssh/*\`, cookie jars).
4. Never echo credential content in errors — scrub to \`type(e).__name__\`.
5. If using kanban-md, \`kanban-md\` is the only board writer.
6. Test execution lives in a sandbox, not the local workspace.
7. One cycle, one step.
8. Human approval required for Done. Loop only moves cards to Review.
9. \`max_cycles: 30\` cap every arming.
10. \`stop_sentinel_path\` never blank at loop start.

---

## Troubleshooting

| Symptom | Check |
|---|---|
| UI icon dim | \`echo \$KIROCREW_AUTONUDGE\` should be \`1\`. |
| Nudge never fires | \`cat ~/.kirocrew/autonudge.json\` — loop \`active:true\`? Right slot_key? |
| Loop fires but agent does nothing | STOP sentinel present? \`ls $STOP_PATH\`. |
| \`code=-15 SIGTERM\` after ~100 cycles | Context-window overflow. Re-arm with \`max_cycles: 30\`. |

Skill reference: \`~/.kirocrew/skills/self-nudge-loop/SKILL.md\` §"Hardened Loop Pattern".
EOF
)

README_MD=$(cat <<EOF
# $PROJECT — planning anchor

Generated by \`self-nudge-loop/scaffold.sh\` on $(date -u +%FT%TZ).

## Files

| File | Purpose |
|---|---|
| \`LOOP.md\` | How to arm / halt the AutoNudge loop. DoD, nudge, kill switches. |
| \`STOP\` (sentinel, absent by default) | \`touch\` to halt the loop next cycle. |
| \`board/\` | kanban-md board (if scaffolded with \`--with-board\`). |

## Before arming the loop

1. Fill in the Definition of Done criteria in \`LOOP.md\` (5 boolean checks).
2. If the project needs one, seed the kanban-md board: \`kanban-md --dir $BOARD_PATH create "..." --status todo\`.
3. Read the skill: \`~/.kirocrew/skills/self-nudge-loop/SKILL.md\` §"Hardened Loop Pattern".

## Non-negotiables

See \`LOOP.md\` §"Operating invariants". Ten rules. Never break them.
EOF
)

write_if_absent "$ANCHOR/LOOP.md"   "$LOOP_MD"
write_if_absent "$ANCHOR/README.md" "$README_MD"

if [[ "$WITH_BOARD" -eq 1 ]]; then
  if command -v kanban-md >/dev/null; then
    if [[ -d "$BOARD_PATH" && "$FORCE" -eq 0 ]]; then
      echo "skip (board exists): $BOARD_PATH"
    else
      kanban-md init --dir "$BOARD_PATH" --name "${PROJECT}-loop" \
        --statuses backlog,todo,in-progress,review,done,archived
      echo "wrote: $BOARD_PATH (kanban-md board)"
    fi
  else
    echo "warn: --with-board requested but 'kanban-md' not on PATH; skipping board init" >&2
  fi
fi

cat <<EOF

Done. Next:
  1. Edit $ANCHOR/LOOP.md — fill the 5 Definition-of-Done criteria.
  2. (Optional) Seed tasks: kanban-md --dir $BOARD_PATH create "..." --status todo
  3. Confirm STOP absent: ls $STOP_PATH
  4. Arm via UI (click 🎯 "Set a goal") or REST (see LOOP.md §"Start the loop (REST)").
EOF
