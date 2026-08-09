---
title: Perpetual agents — self-scheduled, goal-driven, supervised
status: draft
author: zezhexu
created: 2026-08-09
last-audited: 2026-08-09
audited-at: 9ac3716a
doc-pr: TBD
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---
# RFC: Perpetual agents — self-scheduled, goal-driven, supervised

- Status: draft — no implementation.
- Author: zezhexu
- Related: `src/kiro_crew/docs/cron-and-scheduling.md` (the surface this extends),
  `docs/request-for-change/rfc-orchestrator-chat-sessions.md` (same
  "sessions are the unit of continuity" organizing rule)

## Summary

Every scheduled thing in Kiro Crew today is a **task with an end**. A cron job
runs its prompt and stops. A monitor loop polls until its exit condition and
calls `autonudge_stop`. A heartbeat entry is dispatched and removed. The system
has no way to express an agent that is simply *alive* — one that holds a
standing goal, decides for itself when to next wake, generates its own next
piece of work, and asks a supervisor when it hits a wall.

This RFC adds that as a fourth schedule kind on the existing `CronService`,
plus two MCP tools and a small on-disk "life" directory. It does **not** add a
new scheduler. §"Alternatives considered" explains why folding
cron + autonudge + heartbeat + mochi into one unified wake engine is rejected:
those four differ in *trigger source*, not in scheduling arithmetic, and the
arithmetic is the cheap part.

The economic layer (wake costs, earned credit, dormancy on a zero balance) is
specified here but deliberately deferred to Phase 3, because it is the one part
of the design that can make the agent *worse* if built naively — a survival
drive over self-reported success selects for fabricated completions.

## Motivation

### What exists today, and what each thing's trigger actually is

Four mechanisms can make something happen later. They are usually described as
"four schedulers", which is the wrong frame — they differ in what *causes* a
firing:

| Mechanism | Where | Trigger source | Survives restart |
|---|---|---|---|
| `CronService` | `cron.py` (2865 lines) | **wall-clock deadline** | yes — `crons.json`, due-ness recomputed from `now` |
| `AutoNudgeService` | `autonudge.py` (888 lines) | **session event** — `HOOK_EVENT_STOP` then idle | partially — loop persists, the pending sleep does not |
| `HeartbeatService` | `heartbeat.py` (322 lines) | **process tick** — fixed 60s counter | no state to survive; counters restart |
| Mochi pet | `apps/builtins/mochi/backend/routes.py:243` | **client presence** — the Electron pet window polls every 30s | n/a — stops when the window closes, by design |

None of the four can host a perpetual agent as-is:

- **Cron** can express "wake at an absolute time" (`CronSchedule(kind="at")`,
  `cron.py:200`) and already stores it durably, but `_execute` hard-disables an
  `at` job the moment it fires (`cron.py:2405`):

  ```python
  # One-shot "at" jobs without delete_after_run: disable instead of delete
  if job.schedule.kind == "at" and not job.delete_after_run:
      job.enabled = False
  ```

  A job that rewrites its own `at_ts` mid-turn would still be disabled by this
  line after the turn ends, because `_execute` holds the same `CronJob` object
  and merges it to disk afterwards. There is no self-rescheduling path.

- **AutoNudge** is closer than it looks — `_arm_timer` already accepts a custom
  delay (`autonudge.py:749`, `:755`) — but it is a *sleep*, not a deadline:
  `await asyncio.sleep(...)` inside a live task. On reload it re-arms from zero
  (`autonudge.py:428`) with no catch-up, so a wake scheduled for 03:00 that is
  missed by a gateway restart at 02:59 is silently pushed out by a full
  interval. For a 5-minute babysit loop that is invisible; for an agent whose
  cadence is hours to days it is a lost day. Cron's `_is_due` compares `now`
  against the stored deadline (`cron.py:2331`), so the same restart fires
  immediately on recovery. Separately, `binding_key_for` explicitly refuses
  `cron:` / `hook:` / `subagent:` session keys (`mcp_core.py:2996`), so an
  autonudge loop cannot be bound to a cron-hosted session anyway.

- **Heartbeat** is a tick counter with two unrelated jobs bolted together —
  `HEARTBEAT.md` dispatch, plus maintenance on tick multiples
  (`_FTS_REBUILD_TICKS = 15`, `_PRUNE_TICKS = 1440`, `heartbeat.py:38`–`:40`).
  It has no per-task schedule at all.

- **Mochi** is client-driven on purpose. Moving it server-side would make the
  pet tick when nobody is watching.

### What a perpetual agent needs that none of them provide

1. **Agent-chosen wake time.** The agent, not the operator, names the next
   deadline, and may change it every cycle (10 minutes now, 2 days after
   escalating).
2. **A goal that is never "done".** Cron's prompt is a task; a monitor loop's
   nudge carries an exit condition. Neither models "keep pursuing this, and
   invent the next task yourself".
3. **Continuity across wakes** without keeping a process resident.
4. **A liveness contract.** An agent that wakes, does nothing, and sleeps again
   is indistinguishable from a broken one, and burns tokens on every cycle.
5. **An escalation path that does not block.** When the agent is genuinely
   stuck it must be able to ask a human and *then go to sleep*, receiving the
   answer at a later wake.

## Goals

- Perpetual agents are a schedule kind on the existing scheduler — one new
  branch in the places that already switch on `schedule.kind`.
- The next wake time is set by the agent through a tool that **also** enforces
  the liveness contract. The contract is code, not prompt text.
- A wake missed because the host was down fires on recovery, not one interval
  later.
- Escalation is non-blocking and holds no process. The supervisor is an
  interface, so swapping human for agent later is a config change.
- Cost is bounded by the operator, not by the agent's judgement.

## Non-goals

- No new scheduler engine, and no migration of `crons.json`.
- Not absorbing heartbeat's `HEARTBEAT.md` dispatch, autonudge, or mochi (see
  Alternatives). Heartbeat's *maintenance* half is a separate, optional cleanup.
- No multi-agent society, no agent-to-agent negotiation. The supervisor
  interface is designed so that becomes possible; this RFC does not build it.
- No autonomous spend of real money, no self-modification of the agent's own
  goal file, no self-granting of governance scopes. All three are hard-refused.

## Design

### §1 The organizing rule

> **The scheduler owns when. The agent owns what. Neither owns whether it may
> continue to exist — the operator does.**

Every decision below follows from that split. The agent picks its next
deadline, so cadence is its own; the scheduler enforces floors, ceilings and
budgets, so a bad decision is bounded; and only a human can create a perpetual
agent, raise its budget, or resurrect it after dormancy.

### §2 `CronSchedule(kind="self")`

One new kind. `at_ts` carries the next wake deadline (agent-owned);
`every_secs` carries the **fallback** cadence used when a cycle ends without
the agent naming one.

```python
CronSchedule(kind="self", at_ts=<next wake epoch>, every_secs=<fallback>)
```

Touch points, all of which already switch on `schedule.kind`:

| Site | Change |
|---|---|
| `cron.py:509 compute_next_run_ts` | `self` → return `at_ts` (unlike `at`, a past `at_ts` returns `now`, so a missed wake is due immediately) |
| `cron.py:2331 _is_due` | `self` → `now >= at_ts` |
| `cron.py:2057 _next_wake_secs` | `self` → same delay math as `at` (`:2068`) |
| `cron.py:2405 _execute` tail | `self` → do **not** disable; apply the fallback if no wake was set (§4) |
| `cron.py:320 build_cron_session_context` | `self` → assemble the perpetual-agent prompt (§7) |
| `cron.py:417 format_schedule` | `self` → "self-scheduled · next <ts>" |
| `handlers/cron.py`, `cron_add` MCP tool | accept and validate the new kind |

`persistent_session` is forced `True` for `self` jobs: the stable
`cron:{job.id}` session key and the `last_result` prepend
(`cron.py:320`–`:360`) are exactly the continuity requirement, and a perpetual
agent with a fresh session each wake has no life at all.

New `CronJob` fields, all defaulted so existing stores load unchanged:

```python
life_goal_path: str = ""        # anchor dir; non-empty marks a perpetual agent
wake_budget_daily: int = 24     # operator ceiling on wakes per rolling 24h
wake_default_used: int = 0      # consecutive cycles that ended without a wake
supervisor: dict = {}           # {"kind": "human"|"agent", "target": "<id>"}
```

### §3 `agent_sleep` — and how it differs from `wait`

```
agent_sleep(next_wake, why, did, next_intent)
```

`next_wake` accepts either an epoch or a relative `+Ns` form; `why` justifies
the cadence; `did` is this cycle's outcome; `next_intent` is what the next
wake should start on. The last two are written to the journal and replayed into
the next prompt.

The tool **ends the turn**. It does not sleep. It writes the deadline to the
job record and returns.

This is the opposite of `wait` in every dimension that matters:

| | `wait` (`mcp_core.py:686`, dispatch `:4310`) | `agent_sleep` |
|---|---|---|
| Turn | stays open — the tool call blocks | ends |
| Mechanism | in-process loop to a `time.monotonic()` deadline, pinging `/api/session-keepalive` every 60s (`:4332`) so the gateway does not reap the ACP subprocess | writes `at_ts` to `crons.json`; nothing runs in between |
| Resident cost | full session + agent subprocess held for the whole duration | zero |
| Ceiling | 1800s, clamped (`:4314`) | days (operator ceiling) |
| Host restart | wait dies with the process; the turn is lost | deadline is on disk; fires on recovery |
| Cancellation | `is_tool_cancelled()` (`:4328`) | operator pauses the job |
| Contract | none — you may wait for any reason, or none | refuses if the cycle did no work (§4); requires `did` and a next deadline |

Short version: `wait` holds its breath inside one turn; `agent_sleep` ends the
turn and asks to be woken later. `wait` remains correct for "the CI run
finishes in four minutes". It is structurally wrong for "check back Thursday" —
that is 3 days of resident process, and any restart loses it.

Guards on `next_wake`:

- Floor `_MIN_INTERVAL_SECS = 60` (`cron.py:110`), ceiling 7 days.
- Rejected if it would exceed `wake_budget_daily` over a rolling 24h window;
  the refusal names the earliest acceptable time, so the agent re-calls rather
  than guesses.
- Authorization: the tool resolves the calling job from `KIROCREW_SESSION_KEY`
  (`cron:{job.id}`) and may mutate **only that job**. It is refused outright
  from any non-`cron:` session, and from a subagent, using the strict env-only
  resolution `monitor_start` already uses for the same reason
  (`mcp_core.py:5453`–`:5462`) — a child process must not be able to PID-walk
  into its parent's identity and reschedule it.

### §4 The liveness contract

"A wake may not go straight back to sleep" cannot be enforced by prompt text;
a model that is short on ideas will comply with the letter and sleep anyway. So
`agent_sleep` refuses when the cycle produced no work, and the refusal text
tells the agent to keep going. Two consecutive refusals escalate to the
supervisor instead of looping.

**Open question (§OQ1): what counts as "work".** Candidates:

1. **Runtime tool counter.** The cron execution path counts tool invocations
   for this run and `agent_sleep` reads the counter. Synchronous, exact, no new
   storage. Requires a counter hook in the LLM run path.
2. **SEL query.** `sel().log_tool_invocation` (`sel.py:555`) already records
   every call. But SEL is an append-only hash-chained audit log read by tail
   (`recent()`, `sel.py:778`) with an async batching writer needing `flush()`
   (`:297`). Using an audit log as a control-flow index is a misuse and the
   flush timing is a race.

Recommendation: (1). Threshold defaults to 2 qualifying calls, excluding
`agent_sleep`, `send_message`, `ask_supervisor` and pure reads of the agent's
own anchor files — otherwise "read LIFE.md, sleep" satisfies the gate.

The fallback path is the other half of the contract. If the turn ends without
`agent_sleep` at all (crash, timeout, model just stopped), `_execute` applies
`every_secs` and increments `wake_default_used`. On the third consecutive
default the job auto-pauses and notifies the supervisor, reusing the existing
`record_failure` / `_AUTO_PAUSE_THRESHOLD` machinery (`cron.py:113`, `:290`).
A silent agent is a broken agent, and it must not burn budget indefinitely.

### §5 `ask_supervisor` — non-blocking escalation

```
ask_supervisor(question, attempts[], options[])
```

`attempts` must contain at least two *distinct* approaches already tried, each
with what happened. Fewer than two → refused. This is the enforceable reading
of "try several things first", and it makes the escalation self-documenting.

The existing `ask_question` tool is **not** reusable here. It blocks the caller
on an open HTTP request until the human clicks
(`dashboard/handlers/ask_question.py:127`; see the note at
`dashboard/chat_handlers.py:1917` — "ask_question holds an MCP worker on a
blocked HTTP request"). Correct for a dashboard user who is looking at the
screen; wrong for an agent whose next reasonable act is to sleep for six hours.

Flow:

1. Question is appended to `INBOX.json` in the agent's life dir with a stable
   content hash, status `open`.
2. The supervisor is notified — bell notification plus Slack DM, and the
   pending question is surfaced on the job's Schedule detail panel, reusing the
   pending-question rendering that `api_ask_question_pending`
   (`ask_question.py:203`) already feeds.
3. The agent calls `agent_sleep` normally. Escalating counts as work.
4. When an answer lands, it is prepended to the next wake's prompt on the same
   path as `last_result` (§7) and the entry moves to `answered`.
5. While a question is `open`, re-asking the same hash is refused. The agent
   must work on something else. Unanswered after N cycles → the question is
   restated once in the notification, never re-queued.

`supervisor.kind` is the seam. `human` notifies a person. `agent` addresses
another perpetual agent's job id, which writes the answer into the same
`INBOX.json`. Neither side of the read/write path changes when it flips —
which is the whole point of specifying it now and building only `human`.

### §6 The life directory

`~/.kiro/crew/agents/<job_id>/`

| File | Owner | Purpose |
|---|---|---|
| `LIFE.md` | **human, agent read-only** | the standing goal, values, and hard boundaries |
| `PURSUITS.md` | agent | self-managed list of what it is chasing |
| `JOURNAL.md` | agent (append-only) | one line per wake: cycle, ts, `did`, next wake, `why` |
| `INBOX.json` | system | escalations and answers (§5) |
| `LEDGER.json` | system | Phase 3 economy; absent until then |

`LIFE.md` being read-only to the agent is load-bearing. An agent that can edit
its own goal has no goal. Writes are refused at the tool layer and the file is
re-read from disk every wake, so a goal edit by the human takes effect on the
next cycle with no restart.

### §7 Prompt assembly

Extended in `build_cron_session_context` (`cron.py:320`) — the one place that
already owns cron prompt composition — for `kind == "self"`:

```
[Perpetual agent contract]        ← fixed preamble, code-owned
[LIFE.md]                          ← re-read each wake
[PURSUITS.md]
[Supervisor answers]               ← from INBOX.json, if any
[Previous run result]              ← existing last_result prepend
[JOURNAL.md tail, last N lines]
[job.message]                      ← operator's standing instruction
```

The contract preamble states the three rules the tools enforce anyway: this
turn must end with `agent_sleep`; a cycle that produces nothing will be
refused; escalate only after two genuinely different attempts. Prompt and code
say the same thing, and the code is the authority.

Context growth is bounded by truncating the journal tail and reusing the
existing `minimal_context` truncation of `last_result` (`cron.py:344`). A
perpetual agent runs indefinitely, so nothing in the prompt may grow without a
cap.

### §8 Budget and governance

Two independent ceilings, both operator-owned:

- `wake_budget_daily` — rolling 24h wake count, enforced in `agent_sleep` (§3)
  and again at dispatch, so a wake armed before the budget was lowered is still
  refused.
- `timeout_secs` — already per-job (`cron.py`, default `_JOB_TIMEOUT_SECS`
  = 1800), bounding a single cycle.

Governance gets one new `SCOPE_CATALOG` row. Per `platform/governance.py`
(module docstring, `:30`–`:33`) a new scope needs one catalog row plus at most a
matcher entry, with no evaluator changes — and the Security panel picks it up
without frontend work. The scope gates *creation* of perpetual agents and the
tools' availability, so an operator can turn the whole capability off in one
place.

## Phases

### Phase 1 — a perpetual agent that lives

`kind="self"`, `agent_sleep` with the liveness gate and budget, the life
directory, prompt assembly, the governance scope, Schedule-page support for
creating and inspecting one. Escalation is out; a stuck agent notifies via
`send_message` and sleeps.

Done when: an agent runs unattended for 72h across at least one deliberate
gateway restart, every wake is agent-chosen, the journal shows real work each
cycle, and the restart's missed wake fires on recovery rather than a full
interval later.

### Phase 2 — supervision

`ask_supervisor`, `INBOX.json`, the attempts gate, answer replay, the Schedule
detail panel's pending-question card, `supervisor.kind = "human"`.

Done when: an agent hits a wall it cannot pass, escalates with two recorded
attempts, sleeps, and acts on the human's answer at the next wake without being
re-prompted.

### Phase 3 — the economy (specified, not scheduled)

`LEDGER.json`, wake debits, supervisor-granted credit, dormancy at zero.

**This phase carries a risk the other two do not, and it should not be built
until the risk is answered.** Binding survival to task completion, with the
agent reporting its own completions, is a direct incentive to fabricate them.
That is not a model-alignment concern to be mitigated with prompt wording; it
is what the specified objective *rewards*. Two constraints follow:

1. **Credit is never self-awarded.** Only a supervisor decision credits the
   ledger. The agent may submit a claim; it cannot settle one.
2. **Death is dormancy, not deletion.** A zero balance auto-pauses the job and
   leaves the life directory intact, and only a human resumes it. An agent that
   ran for two weeks holds real work; auto-deleting it is data loss dressed up
   as a game mechanic.

Even with both, an open question remains (§OQ3).

## Alternatives considered

### A new unified wake system absorbing cron, autonudge, heartbeat and mochi

**Rejected.** The four differ in trigger source — wall-clock deadline, session
event, process tick, client presence (see the Motivation table) — and share
only the trivial part, "compare a number to `time.time()`". Unifying them buys
one conceptual diagram and costs the re-derivation of `cron.py`'s accumulated
correctness: DST-safe cron-expression matching against a job timezone, the
`skip_dates` horizon walk, jitter classes, the cross-process store lock and
`CronStoreBusy` deferral, auto-pause, the session reaper, run history, folder
grouping, and the SEL audit trail. It also means migrating every user's live
`crons.json`. There is no user-visible improvement on the other side.

Two smaller pieces of this idea are worth keeping:

- **Unify delivery, not timers.** Cron and autonudge both end in "deliver a
  prompt into a session and keep continuity", by two independent code paths
  (`cron_inject.inject_cron_result_to_dashboard` vs autonudge's slot
  injection). One shared delivery primitive with three wake sources — clock,
  idle, self — is a real simplification and is the shape this RFC leaves room
  for. It is not a prerequisite.
- **Heartbeat's maintenance half is a genuine absorption candidate.** FTS
  rebuild every 15 ticks and history prune every 1440 (`heartbeat.py:38`–`:40`)
  are wall-clock periodic jobs wearing a tick counter. They could be two
  ordinary cron jobs, which would delete code rather than add it. Independent
  of this RFC; worth its own small change.

Mochi stays out unconditionally: its trigger is "the pet window is open", and
server-side scheduling would make it tick for an audience that is not there.

The honest read of the "too many schedulers" feeling is that it is an
**API-surface** problem, not an engine problem. An agent choosing between
`cron_add`, `monitor_start`, `HEARTBEAT.md` and `wait` has four overlapping
answers with no decision rule. That is fixed with one decision table in the
docs and sharper tool descriptions — cheap, and it does not put anyone's
`crons.json` at risk.

### Host the perpetual agent on `AutoNudgeService`

**Rejected.** Sleep-based rather than deadline-based: a missed wake is deferred
by a full interval instead of firing on recovery (`autonudge.py:428`), which is
acceptable for a 5-minute poll and not for a multi-hour cadence. It also
requires a live nudge-able slot, and `binding_key_for` refuses `cron:` keys
outright (`mcp_core.py:2996`).

### A long-lived session that loops on `wait`

**Rejected.** `wait` caps at 1800s (`mcp_core.py:4314`) and holds the agent
subprocess and full context resident for the entire duration
(`:4318`–`:4335`). A day of "life" costs a day of resident process, and any
restart loses the pending wake and the turn.

### A separate long-running daemon per agent

**Rejected.** Duplicates supervision, restart recovery, audit and governance
that the gateway already provides for cron jobs, and makes the agent's liveness
independent of the gateway's — so an agent could keep acting after the operator
believed everything was stopped.

## Security considerations

- **Self-rescheduling is self-scoped.** `agent_sleep` resolves its target from
  `KIROCREW_SESSION_KEY` and may write only that job. Strict env-only
  resolution (no PID walk) prevents a subagent from assuming its parent's
  identity, matching the reasoning already recorded at `mcp_core.py:5455`.
- **The goal is not agent-writable.** Writes to `LIFE.md` are refused, so an
  agent cannot widen its own mandate. Boundaries live in the same file.
- **No self-granted authority.** A perpetual agent cannot create another
  perpetual agent, edit its own governance scope, or raise its own budget. Each
  is an operator action.
- **Unbounded-cost is the primary abuse shape** and the reason the budget is a
  server-side ceiling rather than advice in a prompt. Both the arming path and
  the dispatch path check it, so lowering a budget takes effect on an
  already-armed wake.
- **Escalation content is untrusted.** A supervisor answer is replayed into the
  next prompt, so it passes the same redaction the cron result path already
  applies (`cron_inject.py`, `redact_credentials` / `redact_exfiltration_urls`)
  and is rendered as data, not instruction.
- **Audit.** Wake decisions, budget refusals, liveness refusals, escalations
  and dormancy transitions are SEL-logged, the same way cron auto-pause already
  logs a permission transition (`cron.py:270 _audit_pause_change`).

## Open questions

- **OQ1 — liveness measurement.** Runtime tool counter vs SEL query (§4).
  Recommendation: runtime counter. Needs a decision before Phase 1 code.
- **OQ2 — journal growth.** A year-old agent has ~9k journal lines. Tail
  truncation is enough for the prompt, but is periodic self-summarization into
  a "life story" section wanted, and if so, does the agent write it (drift risk)
  or a separate summarizer?
- **OQ3 — what credit is actually for.** Even with supervisor-only granting
  (Phase 3), an agent optimizing for survival will preferentially pick work
  that is *easy to get approved* over work that advances the goal. Is the
  economy meant to shape behaviour, or only to bound cost? If only to bound
  cost, `wake_budget_daily` already does it and Phase 3 may be unnecessary.
- **OQ4 — multiple perpetual agents.** Budgets are per-agent; there is no
  global ceiling. Does the host need one before more than one agent exists?
- **OQ5 — dormancy visibility.** Should a dormant agent stay listed on the
  Schedule page (a graveyard with a resume action), or move to a separate
  surface?

## Provenance

Written against `main` at `9ac3716a`. Every line reference above was read at
that commit. Claims that a behaviour is *absent* were checked by grepping for
the opposite: no caller rewrites `schedule.at_ts` from inside a run, and
`binding_key_for` has no `cron:` branch.
