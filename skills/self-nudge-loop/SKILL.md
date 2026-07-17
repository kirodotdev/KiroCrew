---
name: self-nudge-loop
description: Build a reactive same-session autonomy loop using KiroCrew's AutoNudgeService. One long-lived dashboard session keeps working toward a goal — when its turn completes and no user input arrives within idle_secs, the service auto-injects a nudge message into the SAME session. Survives tab close, logout, and gateway restart. Use when user says "continuous improvement session", "keep going on its own", "same session loop", "self-nudge", "north star loop", or wants one persistent session that auto-resumes whenever idle. NOT for fresh-session crons, parallel work, or external-system callbacks.
tags: [skill, kirocrew, autonudge, autonomy, loop]
---

# Self-Nudge Loop

## Overview
`AutoNudgeService` (in `kiro_crew.autonudge`) keeps a single dashboard chat slot working toward a goal by re-feeding a nudge message every time the slot goes idle. Unlike `cron_add`, the nudge runs IN the same slot — warm memory, same tools, same conversation history. State persists across gateway restarts via `~/.kirocrew/autonudge.json`.

## Usage
Use when the user wants:
- "continuous improvement" that doesn't die between chats
- one long-lived session that auto-resumes whenever idle
- a north-star / roadmap / tasks anchor-file pattern driving autonomous work
- the loop to survive logout and gateway restart

Do NOT use for:
- work best run in fresh isolated session → use plain `cron_add`
- parallel independent tasks → use `spawn_run`
- external-system callbacks → use `register_hook`

## Feature flag

On by default. To disable:
```bash
export KIROCREW_AUTONUDGE=0   # add to systemd unit Environment= or ~/.kirocrew/env
kirocrew restart
```

When disabled, the REST API returns 503 and the UI popover surfaces the error.

## Core Concepts

**Reactive idle timer** — after every `HOOK_EVENT_STOP` (turn completes), the service arms a per-slot `asyncio` timer for `idle_secs`. If a user message arrives, timer cancels (user wins). If the timer fires, the configured nudge is injected into the slot as a distinctively-styled `msg msg-nudge` bubble and `_run_chat` processes it normally.

**Three files the agent owns** (convention, not enforced):
| File | Role |
|---|---|
| `north_star.md` | Immutable goal. Why the loop exists. |
| `roadmap.md` | Phases / milestones. Agent may edit as it learns. |
| `tasks.md` | Active checklist. Agent checks off / adds items each cycle. |

**Kill switches (any of the below):**
- The looping agent itself calls the `autonudge_stop` MCP tool — preferred, works from inside the loop with no ID lookup needed. The tool identifies the current slot from `KIROCREW_SESSION_KEY` and deletes the bound loop.
- Click the **Stop loop** button in the UI popover.
- Create the configured `STOP` sentinel file — next cycle halts.
- `max_cycles` reached — loop deactivates (not removed, so you can resume).
- `DELETE /api/autonudge/{loop_id}` or `autonudge_svc.remove(id)`.

**Warning:** a STOP sentinel file is ONLY checked if the loop was created with a non-empty `stop_sentinel_path`. If the path is empty, the sentinel file is ignored and nudges keep firing. Prefer the `autonudge_stop` MCP tool for in-loop halting.

**Restart survival** — on `AutoNudgeService.start()`, loops marked `active:true` in `~/.kirocrew/autonudge.json` are reloaded and their idle timers re-armed. No catch-up fire — timer starts fresh from zero after restart.

## How to start a loop

**From the UI (preferred):**
1. Click the `🎯 Set a goal` (bullseye) icon in the chat composer toolbar (lit green when active, dim when off).
2. In the popover: paste your nudge message, set idle seconds (min 15, default 60), set max cycles (0 = unlimited), click **Start loop**.
3. Close the popover. Loop runs in the background. Icon stays lit across tab closes / logins.

**From code (MCP / REST):**

`/api/autonudge` is a **user-scoped** endpoint — it requires a Bearer-style token, not the `X-Internal-Secret` header that loopback MCP tools use. Two-step bootstrap:

```bash
# 1. Exchange the local-machine secret for a user token (loopback only).
SECRET=$(cat ~/.kirocrew/.local_secret)
TOKEN=$(curl -sf -H "X-Local-Secret: $SECRET" \
  "http://127.0.0.1:5476/api/token/local?ttl=1h" \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"])')

# 2. Arm the loop with ?token=$TOKEN (query param; cookie also works in browsers).
curl -sf -X POST "http://127.0.0.1:5476/api/autonudge?token=$TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "slot_key": "chat-3-1777089677",
    "message": "Read ~/project/north_star.md, pick next task, execute.",
    "idle_secs": 60,
    "max_cycles": 30,
    "stop_sentinel_path": "/home/zedmor/project/STOP"
  }'
```

Common 403 traps (all have the same error body `{"error":"Token required"}`):
- Forgetting the `?token=…` query param. `/api/autonudge` does **not** accept `X-Internal-Secret` auth — that header only grants machine-auth for a narrow allowlist (`/api/send-message`, `/api/hooks/agent`, `/api/outbox/notify`, `/api/slack/upload-file`, `/api/spawn`, `/api/lessons`, `/api/taskrunner`) defined in `dashboard/server.py`.
- Calling `/api/token/local` without `X-Local-Secret` — the endpoint is in `_BYPASS_EXACT` (no token needed) but still validates the machine secret via HMAC compare. Missing/wrong secret → `{"error":"invalid secret"}` 403.
- Non-loopback source. Token issuance and most internal paths require `is_loopback(request.remote)` regardless of auth material.

**Slot key** — the `slot_key` in the POST body is your dashboard session ID. Inside MCP subprocess code, read `KIROCREW_SESSION_KEY` (format `dashboard:chat-<N>-<epoch>`) and strip the `dashboard:` prefix.

Endpoints:
| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/token/local` | `X-Local-Secret` header, loopback only | issue a user token for local bootstrap |
| GET | `/api/autonudge` | `?token=…` | list all loops |
| GET | `/api/autonudge/slot/{slot_key}` | `?token=…` | loop bound to slot (or null) |
| POST | `/api/autonudge` | `?token=…` | start/replace a loop |
| PATCH | `/api/autonudge/{loop_id}` | `?token=…` | edit message / idle / active |
| DELETE | `/api/autonudge/{loop_id}` | `?token=…` | stop and remove |

**Preferred path for agents: use the `autonudge_stop` MCP tool** for self-halt (reads `KIROCREW_SESSION_KEY`, looks up the bound loop, DELETEs it — no token handling needed on your side). The REST flow above is for external scripts, new-loop arming, and debugging.

## Per-cycle agent behaviour (to put in your nudge message)

Good nudges tell the agent to:
1. Check for a halt condition (STOP sentinel, goal achieved, unrecoverable error). If halt → call the `autonudge_stop` MCP tool with a brief reason, then return. Do NOT just stop working silently: if the AutoNudgeService loop isn't removed, it will keep firing nudges every `idle_secs` and eventually blow the context window.
2. Read north_star.md, roadmap.md, tasks.md (cheap, keeps context fresh).
3. Pick the single highest-leverage next step.
4. Execute it (suggest ≤5 tool calls per cycle).
5. Update tasks.md (check off / add / reorder).
6. Append a one-line entry to a `## Cycle Log` section of tasks.md.
7. Stay silent unless phase boundary / blocker / user-facing decision.

## Common Mistakes

**Forgetting the feature flag** — service loads but does nothing. Check `kirocrew logs | grep AutoNudge` for the "disabled" message.

**idle_secs too short** — sub-30s nudges thrash context. 60s is the sweet spot.

**No kill switch in the nudge text** — user ends up hunting the loop id. Always instruct the agent to check a STOP sentinel.

**Expecting catch-up fires across restart** — timer resets to zero on restart. If the gateway was down when the nudge should have fired, it waits `idle_secs` again before firing. Design around this, don't rely on strict cadence.

**Agent re-pings the same blocker every cycle** — explicitly tell it to post a blocker ONCE, then stay silent until it clears.

**Multiple loops on same slot** — the service enforces one-loop-per-slot. Starting a new one replaces the old one.

---

## Hardened Loop Pattern (production recipe)

Lessons from shipping two long-running loops (RRL self-improvement + HAA verification). Adopt this pattern any time a loop will run overnight or unattended.

### 1. Definition of Done, not "stop when I say"

A loop without a self-retire rule becomes a forever-firing context-eater. Every loop's anchor doc (usually `LOOP.md` next to the project) MUST have a "Definition of Done" section listing N auditable boolean criteria. The nudge message MUST instruct the agent to evaluate these criteria first, and call `autonudge_stop(reason="definition of done met")` when all are true.

Template:

```markdown
## Definition of Done

The loop retires itself when **all** of these are true:

1. <boolean criterion 1 — shell-checkable if possible>
2. <boolean criterion 2>
3. <boolean criterion 3>
4. **Zero stale markers.** `grep -rn "TODO\|FIXME\|XXX\|TBD" <planning-tree>` returns empty.
5. **Board drained.** No cards in todo/in-progress/review (only backlog/done/archived).

When all true: agent posts the DoD checklist with ticks, calls `autonudge_stop(reason="DoD met")`, stays silent.
```

### 2. Three overnight-failure mitigations — bake them into every arming

| Failure | Mitigation (REQUIRED) |
|---|---|
| **Context-window overflow after ~100 cycles** (each nudge+reply adds ~170 B; session SIGTERMs at `ContentWindowOverflow`) | `max_cycles: 30` per arming. Re-arm manually for more. |
| **STOP sentinel ignored** when `stop_sentinel_path` is `""` at loop start — service fires even when agent halts | Never leave `stop_sentinel_path` blank. Point it at a real path (even if the file doesn't exist yet). |
| **Midway cookie leak** via urllib `ValueError` echoing raw `~/.midway/cookie` contents into transcripts | Use `http.cookiejar.MozillaCookieJar(path).load()` + urllib opener, OR `curl -b ~/.midway/cookie`. Scrub auth-path exceptions to `type(e).__name__` only. |

### 3. kanban-md integration (optional but recommended)

If the project has a `kanban-md` board, the nudge MUST instruct the agent to drive it via the CLI (atomic `flock`ed writes), never via `strReplace` on task `.md` files.

Minimal kanban-md ops the agent needs:

```bash
BOARD=<abs path to board dir>
kanban-md --dir $BOARD list --status todo --json   # read
kanban-md --dir $BOARD pick --assignee loop-<n>    # atomic claim next unblocked todo
kanban-md --dir $BOARD show <id>                   # read spec
kanban-md --dir $BOARD edit <id> --add-body "…"    # append progress note
kanban-md --dir $BOARD handoff <id> --notes "…"    # move to review + notes
```

### 4. Hardened nudge template

Replace `<PROJECT>`, `<ANCHOR>`, `<STOP_PATH>`, and `<BOARD_PATH>` for your project. This template encodes all failure mitigations above.

```
Continue the <PROJECT> loop.
Definition of Done: <ANCHOR>/LOOP.md §Definition of Done.

STOP / EXIT CHECKS (every cycle, in order, before anything else):
1. If <STOP_PATH> exists: call autonudge_stop(reason="sentinel"), post "Loop halted by sentinel.", do nothing else.
2. If the Definition-of-Done criteria in LOOP.md are all met: call autonudge_stop(reason="DoD met"), post the DoD checklist with ticks, stop.

BOARD (skip this block if the project has no kanban-md board):
3. `kanban-md --dir <BOARD_PATH> list --status todo --json` — read todo column.
4. `kanban-md --dir <BOARD_PATH> pick --assignee loop-<cycle_n>` — atomic claim of next unblocked todo.
5. If pick returns nothing and a dep-met backlog card exists, promote: `kanban-md --dir <BOARD_PATH> move <id> --status todo` then pick.
6. If everything blocked AND you already posted a blocker this arming: autonudge_stop(reason="all blocked") and stop.

EXECUTE (≤5 tool calls per cycle, hard cap):
7. Read the single spec file for the claimed task.
8. Do ONE atomic thing: draft a fix, write a test, run a test, invoke one SOP. Never all at once.
9. NEVER git push. NEVER destructive ops. NEVER reply to real production tickets during verification — sandbox / read-only only.
10. Midway auth: http.cookiejar.MozillaCookieJar(path).load() + urllib opener, OR `curl -b ~/.midway/cookie -f -s`. NEVER read ~/.midway/cookie as text. NEVER echo cookie contents in any error — scrub exceptions to type(e).__name__.

RECORD:
11. Append progress to the claimed card (kanban-md edit --add-body) or to the anchor doc's Cycle Log section.
12. **DM the owner a tick via `send_message`** (owner DM is default — no channel arg). Template:
    `🎯 <PROJECT> cycle-<n> · <task-id>  Done: <1-line>  Next: <1-line>  Status: <col>`
    Exactly one DM per productive cycle. Skip on halted / no-op cycles.
13. If task complete: handoff to Review (NOT Done — human approves Done).

STAY SILENT in the chat panel unless:
- Phase boundary reached / DoD met (then autonudge_stop + summary).
- Hard blocker needs user decision.
- STOP sentinel tripped.
(send_message DMs to the owner are expected every productive cycle — that is the progress channel, not the chat.)

One cycle = one step. Compound cycles build features.
```

### 5. Arming checklist (do not skip)

Before clicking 🎯 "Set a goal" → Start loop:

- [ ] Anchor doc (`LOOP.md`) exists with a Definition of Done section. (Run `scaffold.sh` in this skill dir to generate a hardened template in one command.)
- [ ] STOP sentinel absent: `ls <STOP_PATH>` says "No such file".
- [ ] Popover fields: nudge (from template), `idle_secs=60`, `max_cycles=30`, `stop_sentinel_path=<STOP_PATH>` — **no blank fields**.
- [ ] If arming via REST instead of the UI: remember the two-step token flow — `GET /api/token/local` with `X-Local-Secret` header first, then `POST /api/autonudge?token=…`. `X-Internal-Secret` alone **will not work** (returns 403 `Token required`).
- [ ] Auth fresh if the loop touches authenticated APIs: `mwinit -l` / `ada credentials update` as needed.

### 6. Ten invariants every loop must respect

1. Never `git push`. Humans push.
2. Never run destructive ops.
3. Never read credential files as text (`~/.midway/cookie`, `~/.aws/*`, `~/.ssh/*`).
4. Never echo credential content in errors — scrub to `type(e).__name__`.
5. If the project has a kanban-md board, `kanban-md` is the only board writer.
6. Test execution lives on a DevSpace or sandbox — never in the local workspace for ops that touch live systems.
7. One cycle, one step. Compound cycles build features.
8. Human approval required for Done. Loop only moves cards to Review.
9. `max_cycles: 30` cap every arming. Re-arm manually for more.
10. `stop_sentinel_path` never blank at loop start.
