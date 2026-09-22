---
name: self-nudge-loop
description: Build a deadline-preserving same-session autonomy loop using Kiro Crew's AutoNudgeService. One long-lived session keeps working toward a goal; each completed loop turn schedules the next nudge, while user turns defer but do not reset that deadline. Survives tab close, logout, and gateway restart. Use when user says "continuous improvement session", "keep going on its own", "same session loop", "self-nudge", or "north star loop". NOT for fresh-session crons, parallel work, or external-system callbacks.
tags: [skill, kirocrew, autonudge, autonomy, loop]
---

# Self-Nudge Loop

## Overview
`AutoNudgeService` (in `kiro_crew.autonudge`) keeps a single chat session working toward a goal by re-feeding a nudge on its persisted deadline, deferred while a user turn is active. Unlike `cron_add`, the nudge runs in the same session — warm memory, same tools, same conversation history. State persists across gateway restarts via `~/.kiro/crew/autonudge.json`.

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
export KIROCREW_AUTONUDGE=0   # add to systemd unit Environment= or ~/.kiro/crew/env
kirocrew restart
```

When disabled, the REST API returns 503 and the UI popover surfaces the error.

## Core Concepts

**Deadline-preserving timer** — after a loop turn completes, the service stores an absolute `next_due_ts` and arms a per-slot timer. A user turn cancels the pending timer task so it cannot race the human, but does not move the deadline; when that turn ends, the timer resumes toward the same deadline and may fire shortly afterward if already due. A delivered loop turn starts the next full interval from that turn's end.

**Three files the agent owns** (convention, not enforced):
| File | Role |
|---|---|
| `north_star.md` | Immutable goal. Why the loop exists. |
| `roadmap.md` | Phases / milestones. Agent may edit as it learns. |
| `tasks.md` | Active checklist. Agent checks off / adds items each cycle. |

**Kill switches (any of the below):**
- The looping agent itself calls the session-bound `autonudge_stop` MCP tool — preferred, with no loop ID or token handling needed. Ordinary prompt loops are removed; structured monitor records follow their own retained-stop contract.
- Click the **Stop loop** button in the UI popover.
- Create the configured `STOP` sentinel file — next cycle halts.
- `max_cycles` reached — loop deactivates (not removed, so you can resume).
- `DELETE /api/autonudge/{loop_id}` or `autonudge_svc.remove(id)`.

**Warning:** a STOP sentinel file is ONLY checked if the loop was created with a non-empty `stop_sentinel_path`. If the path is empty, the sentinel file is ignored and nudges keep firing. A path pointing at a sensitive location is refused at arm time, and a persisted path is re-homed onto the current data home on reload — silently dropped if it cannot be repaired. Prefer the `autonudge_stop` MCP tool for in-loop halting.

**Overlap with `babysit`:** the bundled **babysit** skill is the `monitor_*`-native
guide for same-session loops and is the one to reach for when the job is watching a
PR, a CI run, a ticket, or a deployment. This skill covers the scaffolded
goal/roadmap/tasks pattern and the raw service surface underneath it. Use one
vocabulary: `interval_secs` and `max_cycles` as the MCP tools name them.

**Restart survival** — on `AutoNudgeService.start()`, loops marked `active:true` in `~/.kiro/crew/autonudge.json` are reloaded and re-armed from their persisted `next_due_ts`. A lost best-effort deadline write degrades to one fresh interval after restart; the normal path does not reset the countdown.

## How to start a loop

**From an agent session (preferred):** call the MCP tools. `monitor_start(message,
interval_secs?, gate?, max_cycles?, max_runtime_secs?, banner?)` arms a loop on the
calling session, `monitor_update(message?, interval_secs?, max_cycles?,
max_runtime_secs?)` revises it in place without losing its cycle count, and
`autonudge_stop()` halts it. No token handling is involved. The tool's
`interval_secs` (default 300) is stored as the loop's `idle_secs`; the raw
REST/dataclass field defaults to 60.

`monitor_start` defaults to 24 delivered cycles and a 14,400-second wall-clock
budget; use explicit positive finite bounds for unattended work. Raw REST defaults
both bounds to 0 (unlimited). Reaching either bound is a runaway backstop, not a
successful finish: check the exit condition every cycle and call `autonudge_stop`
deliberately.

**From the UI:**
1. Click the `🎯 Set a goal` (bullseye) icon in the chat composer toolbar (lit green when active, dim when off).
2. In the popover: paste your nudge message, set idle seconds (min 15, default 60), set max cycles (0 = unlimited), click **Start loop**.
3. Close the popover. Loop runs in the background. Icon stays lit across tab closes / logins.

**From an external script or when debugging (REST, human-operated only):**

`/api/autonudge` is a **user-scoped** endpoint. Agents should use the MCP tools
above and must not read, print, or request local-secret/token material. If a human
needs the raw REST surface, obtain a short-lived dashboard token in a private
terminal (for example with `kirocrew token`) and keep it out of chat and logs:

```bash
# Human-controlled terminal only. Obtain this without exposing it to an agent.
TOKEN='<short-lived dashboard token>'

curl -sf -X POST "http://127.0.0.1:5476/api/autonudge?token=$TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "slot_key": "chat-3-1777089677",
    "message": "Read ~/project/north_star.md, pick next task, execute.",
    "idle_secs": 60,
    "max_cycles": 30,
    "max_runtime_secs": 14400,
    "stop_sentinel_path": "/home/user/project/STOP"
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

**`interval_secs` too short** — sub-30s nudges thrash context. 300s is the value to use for external polling (CI, review bots); reserve 60s for tight local iteration.

**No kill switch in the nudge text** — user ends up hunting the loop id. Always instruct the agent to check a STOP sentinel.

**Expecting the interval to reset across restart** — the persisted `next_due_ts` normally survives and is re-armed. A fresh interval occurs only when the best-effort deadline write was lost. Do not depend on a restart to postpone a due cycle.

**Agent re-pings the same blocker every cycle** — explicitly tell it to post a blocker ONCE, then stay silent until it clears.

**Multiple loops on the same slot** — the service enforces one automation per session. `monitor_start` is create-only and refuses while an active loop or structured monitor exists; revise the bound prompt loop with `monitor_update` instead of re-arming it.

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
| **STOP file does not stop before delivery** when no service sentinel was configured | For agent/UI arming, put the STOP-path check in the nudge and call `autonudge_stop`; the UI does not expose `stop_sentinel_path`. Raw REST callers may additionally set the field for a pre-delivery check. |
| **Credential-file leak** via urllib `ValueError` echoing raw cookie-jar contents (e.g. `~/.config/<app>/credentials`) into transcripts | Use `http.cookiejar.MozillaCookieJar(path).load()` + urllib opener, OR `curl -b <cookie-jar>`. Scrub auth-path exceptions to `type(e).__name__` only. |

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
10. Cookie-jar auth: http.cookiejar.MozillaCookieJar(path).load() + urllib opener, OR `curl -b <cookie-jar> -f -s`. NEVER read a credential/cookie file as text. NEVER echo cookie contents in any error — scrub exceptions to type(e).__name__.

RECORD:
11. Append progress to the claimed card (kanban-md edit --add-body) or to the anchor doc's Cycle Log section.
12. Notify only on a real phase boundary, blocker, threshold crossing, or completion. Use `send_message` with the intended destination when conversational delivery is required; an omitted destination produces a dashboard notification, not an owner DM. Do not emit a routine per-cycle tick.
13. If task complete: handoff to Review (NOT Done — human approves Done).

STAY SILENT in the chat panel unless:
- Phase boundary reached / DoD met (then autonudge_stop + summary).
- Hard blocker needs user decision.
- STOP sentinel tripped.

One cycle = one step. Compound cycles build features.
```

### 5. Arming checklist (do not skip)

Before clicking 🎯 "Set a goal" → Start loop:

- [ ] Anchor doc (`LOOP.md`) exists with a Definition of Done section. (Run `scaffold.sh` in this skill dir to generate a hardened template in one command.)
- [ ] STOP sentinel absent: `ls <STOP_PATH>` says "No such file".
- [ ] Popover fields: nudge (from template), `idle_secs=60`, and a finite `max_cycles` such as 30. The UI does not expose `stop_sentinel_path`, so the nudge itself must check `<STOP_PATH>` and call `autonudge_stop`.
- [ ] If arming with `monitor_start`, set positive finite `max_cycles` and `max_runtime_secs`; use `monitor_update` rather than a second arm when the instruction changes.
- [ ] If a human is arming via REST, keep the dashboard token in their private terminal and optionally set `stop_sentinel_path`; agents must not read local-secret or cookie files.
- [ ] If the loop touches authenticated APIs, the user must establish or refresh access through the supported client; never copy credential contents into the nudge or transcript.

### 6. Ten invariants every loop must respect

1. Never `git push`. Humans push.
2. Never run destructive ops.
3. Never read credential files as text (`~/.aws/*`, `~/.ssh/*`, cookie jars).
4. Never echo credential content in errors — scrub to `type(e).__name__`.
5. If the project has a kanban-md board, `kanban-md` is the only board writer.
6. Test execution lives in a sandbox — never in the local workspace for ops that touch live systems.
7. One cycle, one step. Compound cycles build features.
8. Human approval required for Done. Loop only moves cards to Review.
9. `max_cycles: 30` cap every arming — this recipe's own recommendation, not a code default (`monitor_start` defaults to 24, the raw surface to 0 = unlimited). Re-arm manually for more.
10. Every nudge checks an explicit halt condition and calls `autonudge_stop`; only raw REST arming can additionally configure `stop_sentinel_path`.
