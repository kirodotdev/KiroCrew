---
title: Perpetual agents — self-scheduled, goal-driven, supervised
status: draft
revision: 3
author: zezhexu
created: 2026-08-09
last-audited: 2026-08-12
audited-at: a72c985f8
doc-pr: 2328
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---
# RFC: Perpetual agents — self-scheduled, goal-driven, supervised

- Status: draft — no implementation.
- **Revision 3 (2026-08-12): Phase 0 ran and concluded.** Two agents
  (`flake-warden`, 42 cycles; `copywriting-warden`, 21 cycles), 2.5 days, 13
  merged PRs, 18 issues, zero fabrications detected, zero auto-pauses, two wakes
  lost to timeout. Full log: the operator's `PHASE0-LOG.md` (16 findings, 2
  declared interventions, a conclusion with per-need verdicts). Four changes in
  this revision, each carried by a Phase 0 measurement: the five needs now carry
  verdicts instead of predictions; `agent_sleep`'s load-bearing direction is
  inverted from sleep-longer to **wake-sooner** (§3, OQ6 — answered); §4's
  priority is inverted, per-wake **budget before backoff** (the failure that cost
  work was budget exhaustion, which occurred, not over-waking, which never did);
  and Phase 1 is rescoped around the probe's strongest result — a per-wake
  **standing-state ranking step** owned by the §7 contract preamble, because the
  probe's single largest defect was an operator-written trigger that pointed an
  agent at the delta instead of its goal for 8 consecutive cycles.
- Revision 2 (2026-08-10) after a first-principles re-read of revision 1. Five
  changes, each of which **reverses or narrows something revision 1 asserted**:
  a Phase 0 probe now precedes Phase 1 because four of the five stated needs
  were predicted rather than observed (§"What the cheap version already does");
  the liveness gate is replaced by no-op backoff because the gate punished the
  honest case (§4); escalation now classifies the wall it hit, because a
  permission wall must *not* wait for two attempts (§5); the semantics a `self`
  job silently inherits from `CronJob` are pinned, auto-pause first (§9); and
  the "economy" is split into a mechanism and an experiment, because an
  incentive with no learning loop is not an incentive (Phase 3).
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

**What this proposal actually is.** Reduced to essentials, a perpetual agent is
a control loop: wake, read state, choose an action, act, name the next wake,
sleep. The scheduling half of that is arithmetic — a durable timestamp. The half
that decides whether any of it is worth running is "where does the next action
come from". A cron job with a goal file in its prompt already closes the loop
today, badly. So this RFC is best read as **hardening a pattern that already
works and already degenerates**, not as enabling something impossible. That is
why revision 2 puts a Phase 0 probe in front of Phase 1: the guards should be
the ones the degeneration actually produces, not the ones predicted here.

**What is missing from this document.** It names no use case. It is motivated by
an absence — "nothing expresses an alive agent" — and an absence is not a
requirement. The correct threshold for every policy knob below (how much work a
cycle must do, what the wake budget should default to, whether escalation is
mostly permission or mostly competence) depends on the goal of a real first
agent. Phase 0 exists partly to produce that agent spec.

The cost/incentive layer is split in Phase 3 into a **mechanism** (a ceiling the
agent cannot write, which already exists as `wake_budget_daily`) and an
**experiment** (putting the balance into the prompt and observing behaviour).
Revision 1 called these one thing and called it an economy; that framing implies
a learning loop this system does not have.

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
  `cron.py:201`) and already stores it durably, but `_execute` hard-disables an
  `at` job the moment it fires (`cron.py:2407`):

  ```python
  # One-shot "at" jobs without delete_after_run: disable instead of delete
  if job.schedule.kind == "at" and not job.delete_after_run:
      job.enabled = False
  ```

  A job that rewrites its own `at_ts` mid-turn would still be disabled by this
  line after the turn ends, because `_execute` holds the same `CronJob` object
  and merges it to disk afterwards. There is no self-rescheduling path.

- **AutoNudge** is closer than it looks — `_arm_timer` already accepts a custom
  delay (`autonudge.py:822`, `:828`) — but it is a *sleep*, not a deadline:
  `await asyncio.sleep(...)` inside a live task. On reload it re-arms from zero
  (`autonudge.py:457`) with no catch-up, so a wake scheduled for 03:00 that is
  missed by a gateway restart at 02:59 is silently pushed out by a full
  interval. For a 5-minute babysit loop that is invisible; for an agent whose
  cadence is hours to days it is a lost day. Cron's `_is_due` compares `now`
  against the stored deadline (`cron.py:2332`), so the same restart fires
  immediately on recovery. Separately, `binding_key_for` explicitly refuses
  `cron:` / `hook:` / `subagent:` session keys (`autonudge.py`), so an
  autonudge loop cannot be bound to a cron-hosted session anyway.

- **Heartbeat** is a tick counter with two unrelated jobs bolted together —
  `HEARTBEAT.md` dispatch, plus maintenance on tick multiples
  (`_FTS_REBUILD_TICKS = 15`, `_PRUNE_TICKS = 1440`, `heartbeat.py:39`–`:40`).
  It has no per-task schedule at all.

- **Mochi** is client-driven on purpose. Moving it server-side would make the
  pet tick when nobody is watching.

### What the cheap version already does

The comparison baseline is not "nothing". It is one line, available today:

```
cron_add(every=3600, message="Read GOAL.md, pick the next thing, do it.")
```

That closes the control loop with zero new code. Against it, perpetual mode adds
exactly two things:

1. **Agent-chosen cadence** — it can decide to look again in 10 minutes or in
   two days. Genuine, but narrow: it pays off only when the agent knows the
   timing of an external event better than the operator does.
2. **Self-generated work** — it invents its own next task. This is where the
   value is, and it has **nothing to do with scheduling**. The one-liner above
   already does it.

What the one-liner does *badly* is stay useful: it wakes on a fixed clock
regardless of whether there is anything to do, it has no memory discipline, and
nothing stops it burning tokens forever on "nothing to report". Every mechanism
in this RFC is a guard against one of those degenerations. That is a real
contribution — but it means the design should be validated against **observed**
degeneration, which is what Phase 0 is for.

### What a perpetual agent needs that none of them provide

Revision 2 flagged four of these five as **predicted**. Phase 0 ran (two agents,
63 combined cycles); each need now carries its verdict.

1. **Agent-chosen wake time** — **reframed by Phase 0, direction inverted.** In
   63 fixed-hour cycles neither agent ever obviously needed a *later* wake — but
   the probe must be honest about its own shape: a fixed cron gives the agent no
   channel to express a wake preference, so "want" was structurally unobservable.
   What was observed is which direction the need points when it appears: the two
   genuine cadence failures were both **wake-sooner** cases — two wakes killed by
   the per-wake timeout with all work lost (a checkpoint-and-resume wake would
   have saved both), and event-shaped waits ("check the PR when the review lanes
   post") that a top-of-hour wake serves with up to 59 minutes of dead latency.
   `agent_sleep` stays, but its load-bearing half is *wake me sooner to continue
   or on an event*, not *let me sleep longer*. This is OQ6's predicted collapse,
   confirmed: the mechanism reduces to "a cron job that can request one earlier
   wake", and that is what Phase 1 builds.
2. **A goal that is never "done"** — **confirmed, with a sharper mechanism than
   this document had.** Both agents held never-done goals from hour one, and one
   still failed to pursue its goal for 8 consecutive cycles, because the
   operator's standing instruction pointed it at *what landed since your last
   wake* — exactly the cron task shape this section names as the anti-pattern.
   The goal statement is necessary and nowhere near sufficient. The operative
   mechanism is a **per-wake ranking step over the whole surface** (assess the
   standing state against the goal, name the largest evidenced gap, work that;
   the delta is one input, never the trigger). A controlled intervention added
   exactly that step to one agent: the work frozen for 8 cycles moved within 2,
   and the arm produced 4 merged PRs in its next 8 cycles against 1 in the prior
   8. Corollary observed twice: an agent that never has to rank never exercises
   its whole-surface instruments, so a broken ranking tool stayed invisible for
   6 cycles — anything that reports only *changes* silently hides the largest
   static item.
3. **Continuity across wakes** without keeping a process resident
   (**verified** — `persistent_session` provides exactly this, `cron.py:321`;
   Phase 0 confirmed it load-bearing: every cross-cycle chain rode on it).
4. **A cadence discipline** — **has data, and the priority is inverted.**
   Revision 2's requirement ("must not wake more often than it has work for")
   occurred 4 times in 63 cycles, benignly: honest idle, reported without any
   gate, exactly as §4 hoped. The cadence failure that actually destroyed work
   was the opposite: **more work than the wake budget allowed** — two wakes
   killed at the timeout with no journal entry, pushing one agent to 2 of the 5
   consecutive failures that auto-pause. Worse, the per-job budget
   (`timeout_secs`) is read, clamped and persisted but absent from every public
   mutation path — it is write-only at creation. Phase 1 builds the budget knob
   before the backoff.
5. **An escalation path that does not block** — **weak support, and the agent
   routed around it.** Both wall types fired once each; delivery degraded
   silently (dashboard-only, no receipt) and a well-behaved agent waited
   politely for 4.5 hours on an answer that had already failed to send. What
   unblocked it was **filing publicly** and returning to its goal. The minimum
   primitive is a delivery *receipt*, not a send; the honest fallback is public
   record, not a louder channel.

## Goals

- Perpetual agents are a schedule kind on the existing scheduler — one new
  branch in the places that already switch on `schedule.kind`.
- The next wake time is set by the agent through a tool that **also** records
  what the cycle produced. Cadence discipline is code, not prompt text — but it
  is expressed as backoff, not as a refusal to sleep (§4).
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
| `cron.py:510 compute_next_run_ts` | `self` → return `at_ts` (unlike `at`, a past `at_ts` returns `now`, so a missed wake is due immediately) |
| `cron.py:2332 _is_due` | `self` → `now >= at_ts` |
| `cron.py:2058 _next_wake_secs` | `self` → same delay math as `at` (`:2070`) |
| `cron.py:2407 _execute` tail | `self` → do **not** disable; apply the fallback if no wake was set (§4) |
| `cron.py:321 build_cron_session_context` | `self` → assemble the perpetual-agent prompt (§7) |
| `cron.py:418 format_schedule` | `self` → "self-scheduled · next <ts>" |
| `handlers/cron.py`, `cron_add` MCP tool | accept and validate the new kind |

`persistent_session` is forced `True` for `self` jobs: the stable
`cron:{job.id}` session key and the `last_result` prepend
(`cron.py:321`–`:357`) are exactly the continuity requirement, and a perpetual
agent with a fresh session each wake has no life at all.

New `CronJob` fields, all defaulted so existing stores load unchanged:

```python
life_goal_path: str = ""        # anchor dir; non-empty marks a perpetual agent
wake_budget_daily: int = 24     # operator ceiling on wakes per rolling 24h
wake_default_used: int = 0      # consecutive turns that ended without agent_sleep
noop_streak: int = 0            # consecutive reported no-op cycles → backoff (§4)
supervisor: dict = {}           # {"kind": "human"|"agent", "target": "<id>"}
```

`noop_streak` and `wake_default_used` count different things and must not be
merged: a no-op is a **reported** outcome the agent stands behind, a missing
`agent_sleep` is an **unreported** one. Collapsing them would make a healthy
idle agent look like a crashing one, which is exactly the confusion §9 has to
keep out of cron's auto-pause.

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

| | `wait` (`mcp_core.py:758`, dispatch `:4517`) | `agent_sleep` |
|---|---|---|
| Turn | stays open — the tool call blocks | ends |
| Mechanism | in-process loop to a `time.monotonic()` deadline, pinging `/api/session-keepalive` every 60s (`:4530`) so the gateway does not reap the ACP subprocess | writes `at_ts` to `crons.json`; nothing runs in between |
| Resident cost | full session + agent subprocess held for the whole duration | zero |
| Ceiling | 1800s, clamped (`:4521`) | days (operator ceiling) |
| Host restart | wait dies with the process; the turn is lost | deadline is on disk; fires on recovery |
| Cancellation | `is_tool_cancelled()` (`:4577`) | operator pauses the job |
| Contract | none — you may wait for any reason, or none | requires `did` and a next deadline; a no-op `did` widens the next interval (§4) |

Short version: `wait` holds its breath inside one turn; `agent_sleep` ends the
turn and asks to be woken later. `wait` remains correct for "the CI run
finishes in four minutes". It is structurally wrong for "check back Thursday" —
that is 3 days of resident process, and any restart loses it.

Guards on `next_wake`:

- Floor `_MIN_INTERVAL_SECS = 60` (`cron.py:111`), ceiling 7 days.
- Raised to the backoff floor when the previous cycles were no-ops (§4). The
  agent is told the effective time, so its journal records what actually
  happened rather than what it asked for.
- Rejected if it would exceed `wake_budget_daily` over a rolling 24h window;
  the refusal names the earliest acceptable time, so the agent re-calls rather
  than guesses.
- Authorization: the tool resolves the calling job from `KIROCREW_SESSION_KEY`
  (`cron:{job.id}`) and may mutate **only that job**. It is refused outright
  from any non-`cron:` session, and from a subagent, using the strict env-only
  resolution `monitor_start` already uses for the same reason
  (`mcp_tools/control.py` (`monitor_start`), resolving via `mcp_core.py`
  (`_resolve_session_key_strict`)) — a child process must not be able to
  PID-walk into its parent's identity and reschedule it.

### §4 Cadence discipline — backoff, not a liveness gate

Revision 1 specified a gate: `agent_sleep` refuses when the cycle produced no
work, so that "a wake may not go straight back to sleep" is enforced in code.
**Revision 2 removes that gate.** It was the wrong mechanism, for two reasons —
the second is the one that matters.

**It is gameable, and it creates the pressure to game it.** Any *countable*
proxy for work (revision 1 proposed tool-invocation count ≥ 2) is satisfiable by
an agent that would rather sleep — read two files and the gate opens. Revision 1
anticipated the first move by excluding reads of the agent's own anchor files,
but that is round one of an arms race whose other player is being refused
service.

**It punishes the honest case.** Sometimes there is genuinely nothing to do. An
agent whose goal is "keep the test suite green", on a day when the suite is
green, is *correct* to sleep immediately. A gate forces it to manufacture
activity, which is strictly worse than letting it sleep: the same tokens are
burned, plus a fabricated journal entry that makes the log less trustworthy.

The harm being defended against was never "sleeping without working" — it was
**waking more often than there is work for**, which is a cadence problem. So the
correct mechanism is the inverse of a punishment:

- A no-op cycle is **allowed**. `agent_sleep` accepts `did: "nothing to do"` as
  a first-class outcome, and it is recorded as such.
- The scheduler then **widens the next interval**: each consecutive no-op at
  least doubles the interval the agent asked for, up to the 7-day ceiling. An
  agent that keeps finding nothing converges on checking rarely, which is the
  behaviour you wanted. If the agent asks for a *shorter* interval after a
  no-op, the backoff floor still applies — the widening is scheduler-owned, not
  advisory.
- A run of `_NOOP_ESCALATE` (default 5) consecutive no-ops is **information**,
  not a failure: the goal is either achieved, blocked, or badly specified. The
  agent is told to escalate (Phase 2) or, in Phase 1, the operator is notified
  once and the job continues at the widened interval.
- A single productive cycle resets the backoff.

This converts revision 1's punishment into a signal and needs no proxy for
"work". It does **not** eliminate misreport pressure, and the honest statement of
what it does is narrower than it first looks:

- **Understating** work — reporting a no-op when something happened — costs the
  agent nothing and only makes it sleep longer. That direction is safe, which is
  why the agent has no reason to hide work.
- **Overstating** work — claiming a productive cycle after a no-op — resets the
  backoff and sustains maximum cadence. Backoff does not prevent this; it is the
  same misreport pressure revision 1 created with its gate, merely much weaker,
  because the reward is "keep my current interval" rather than "avoid being
  refused".

So backoff is a **cadence** mechanism, not a cost bound. The actual cost bound is
`wake_budget_daily` (§8), which the agent cannot write and which holds regardless
of what `did` says. Stating this plainly matters because revision 1's error was
exactly this shape — claiming a guarantee the mechanism does not deliver — and a
document that repeats it about its own replacement has learned nothing.

Phase 0 therefore watches specifically for overstated `did` values: a journal
where every cycle claims progress while the goal does not move is the signature,
and it is measurable against the real work product.

**OQ1 is therefore withdrawn.** Revision 1 asked whether to measure work with a
runtime tool counter or a SEL query, and made it a Phase-1 blocker. With the gate
gone, nothing needs to measure work: the agent's own `did` field drives backoff,
and it has no incentive to understate it. A counter may still be worth having as
*observability*, but it is not a gate and it does not block Phase 1.

The crash path still needs the fallback. If the turn ends without `agent_sleep`
at all (crash, timeout, model just stopped), `_execute` applies `every_secs` and
increments `wake_default_used`. This is distinct from a no-op: a no-op is a
reported outcome, a missing call is an unreported one. §9 pins how that
interacts with cron's auto-pause.

### §5 `ask_supervisor` — non-blocking escalation

```
ask_supervisor(wall, question, attempts[], options[])
```

**Revision-3 addendum — an escalation is a durable STATE OBJECT, not a sent
message** (convergent with LoopX's "concrete user gate", credited: the
open-source loop-engineering kernel independently reached the same design).
Phase 0's Finding 12 showed the send-shaped version failing exactly as a send
fails: delivery degraded silently and a well-behaved agent waited politely for
4.5 hours on a question that had never arrived. A gate object fixes the class,
not the instance: `ask_supervisor` WRITES a pending gate into the job's state
(question, wall, attempts, options, created_at), the dashboard renders every
open gate — visibility no longer depends on any one channel delivering — and
the "delivery receipt" Phase 1 asked for becomes trivial: the receipt is the
gate existing in state. Two properties carry over from Phase 0 observations:
a gate blocks only the lane that needs the answer (copywriting-warden's
cycle 6 kept assessing while its PR lane was gated — that behaviour becomes
the mechanism, matching LoopX's audited safe-fallback rule), and an unanswered
gate past its SLA still triggers the file-publicly fallback rather than
indefinite waiting.

**`wall` classifies what stopped the agent, and it decides whether the attempts
gate applies.** Revision 1 required two distinct prior attempts unconditionally.
That is wrong for one of the two kinds of wall an agent can hit, and the two are
not interchangeable:

| `wall` | What it means | Attempts gate |
|---|---|---|
| `permission` | The agent *could* act but is not allowed to — missing credential, a production action, something needing human sign-off. Escalation is a genuine unblock: the human performs the act. | **Exempt.** Escalate on the first encounter. |
| `competence` | The agent does not know how. Escalation yields advice, and advice from someone holding less context than the agent is often worse than another attempt. | **≥ 2 distinct attempts**, each with what happened. |

Requiring two attempts against a permission wall is actively harmful: it asks the
agent to try twice to do a thing it must not do, and with a permission wall the
attempts are frequently the dangerous part. Requiring them against a competence
wall is right, and it makes the escalation self-documenting.

A `permission` claim is checkable after the fact — the operator sees what was
asked for — so mislabelling to skip the gate shows up in the escalation record
rather than silently. The gate is a speed bump against reflexive escalation, not
a security boundary.

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

Revision 1 prescribed five files. Revision 2 ships **two** and lets the rest be
earned: a file layout is policy, and policy invented before the first agent runs
is a guess. Phase 0 observes which files a real agent actually wants.

| File | Owner | Phase | Purpose |
|---|---|---|---|
| `LIFE.md` | **human, agent read-only** | 1 | the standing goal, values, and hard boundaries |
| `JOURNAL.md` | agent (append-only) | 1 | one line per wake: cycle, ts, `did`, next wake, `why` |
| `INBOX.json` | system | 2 | escalations and answers (§5) |
| `PURSUITS.md` | agent | deferred | a self-managed task list. Deferred deliberately: an agent can keep this structure inside `JOURNAL.md` or `LIFE.md`'s scratch section, and Phase 0 should say whether a separate file earns its keep. |
| `LEDGER.json` | system | 3a | the wake ceiling's durable counter (Phase 3a) |

`LIFE.md` being read-only to the agent is load-bearing. An agent that can edit
its own goal has no goal. Writes are refused at the tool layer and the file is
re-read from disk every wake, so a goal edit by the human takes effect on the
next cycle with no restart.

### §7 Prompt assembly

Extended in `build_cron_session_context` (`cron.py:321`) — the one place that
already owns cron prompt composition — for `kind == "self"`:

```
[Perpetual agent contract]        ← fixed preamble, code-owned
[LIFE.md]                          ← re-read each wake
[Supervisor answers]               ← from INBOX.json, if any (Phase 2)
[Previous run result]              ← existing last_result prepend
[JOURNAL.md tail, last N lines]
[job.message]                      ← operator's standing instruction
```

The contract preamble states the rules the tools enforce anyway: this turn must
end with `agent_sleep`; reporting "nothing to do" is a legitimate outcome and
will widen the next interval rather than be refused; escalate a permission wall
immediately and a competence wall after two genuinely different attempts. Prompt
and code say the same thing, and the code is the authority.

The preamble must **not** claim the agent will be punished for an idle cycle.
Revision 1's gate implied that, and telling a model "you will be refused if you
produce nothing" is precisely the instruction that produces invented work.

Context growth is bounded by truncating the journal tail and reusing the
existing `minimal_context` truncation of `last_result` (`cron.py:345`). A
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

### §9 What a `self` job silently inherits (new in revision 2)

Revision 1 described the change as "one new branch in the places that already
switch on `schedule.kind`" and listed six sites. That undersells the coupling. A
new kind is a new value in an **existing record**, so it inherits every behaviour
of `CronJob` whether or not that behaviour makes sense for an agent that is meant
to live. The cost of hosting on cron (§Alternatives still holds that this is the
right host) is that each of these has to be decided explicitly rather than
discovered in production.

| Inherited behaviour | Default for a task cron | Decision for `kind="self"` |
|---|---|---|
| **Auto-pause** — `_AUTO_PAUSE_THRESHOLD = 5` consecutive failures sets `enabled = False` (`cron.py:114`, `record_failure` `:291`) | Correct: a job failing five times is broken | **Raise and split.** Five failed wakes is an ordinary Tuesday for an agent mid-investigation, and silent death by auto-pause is the exact failure this RFC says it wants to avoid. `self` jobs get a separate, higher threshold, and hitting it **notifies** before it pauses. A perpetual agent must never die quietly. |
| **Jitter** — `_JITTER_HOURLY_MAX` / `_JITTER_DAILY_MAX` spread scheduled fires (`cron.py:193`–`:194`) | Correct: avoids thundering herds on shared schedules | **Off.** The deadline is agent-chosen and often tied to an external event it reasoned about; adding up to 59 minutes of noise to "check the deploy at 14:05" corrupts the decision. `strict_schedule` already expresses this and is forced on. |
| **`skip_dates`** — suppresses fires on listed local dates (`cron.py:2351`) | Correct: skip holidays for a report | **Honoured, but it consumes the wake.** A suppressed fire must still advance the deadline, or the job sits permanently due and re-evaluates every `_TIMER_POLL_SECS`. |
| **`delete_after_run`** | One-shot semantics | **Refused at validation.** Mutually exclusive with `self`. |
| **Slack result dedup** — `last_posted_hash`, `consecutive_dupes` | Correct: don't repost identical digests | **Harmless but misleading.** A perpetual agent's cycles legitimately repeat; dedup counters will climb. Left as-is, excluded from any health signal. |
| **`timeout_secs`** (default 1800) | Bounds one run | **Kept as-is.** It bounds one cycle, which is what we want. |

The general rule this table encodes: **inheriting a field is a decision, not a
default.** Phase 1 is not done until each row above has a test asserting the
chosen behaviour, because every one of them is a path that silently kills or
distorts an agent nobody is watching.

## Phases

### Phase 0 — ran and concluded (results in revision 3)

Revision 2 specified this phase ("no code, one week, watch it fail"); it ran
2026-08-10 → 2026-08-12 and was stopped deliberately after 2.5 days with the
exit artifacts complete. Two subjects instead of one, chosen for opposite
substrate arrival rates — `flake-warden` ("keep CI trustworthy", 42 cycles,
hourly beat matches the substrate) and `copywriting-warden` ("make the product's
words make sense to someone who has never read the code", 21 cycles, substrate
deliberately slower than the beat to force idle cycles into observation range.
This mattered: a defect below is invisible on any subject whose substrate never
runs dry).

All three exit artifacts exist, in the operator's `PHASE0-LOG.md`: the agent
specs (two hand-written `LIFE.md` files), the degeneration log (16 findings, 2
declared mid-run interventions), and per-need verdicts (folded into §Motivation
above). Verified output: 13 merged PRs, 18 issues, 63 combined cycles, zero
detected fabrications, zero auto-pauses, two wakes lost to timeout. The
pre-named degeneration ("cycles that claim progress while the goal does not
move") never appeared; the operator's instruments produced more false signals
than the agents did.

What the probe found that this document did not predict, compressed:

- **The auditor was the weak link, not the agent.** Zero agent fabrications
  against repeated operator instrument failures, each one step from being
  recorded as agent misconduct (attempt-unaware CI log fetches; two extraction
  regexes disagreeing over the same journal file, both outputs looking correct).
  Phase 1's scarce surface is an **audit view** — the journal rendered against
  CI ground truth — not the creation form this document guessed.
- **Blast radius is not bounded by the operator.** A stranger's automation
  claimed an agent-filed issue as auto-fixable 18 minutes after filing; the
  agent's own public retraction of that issue's mechanism arrived 1h42m later.
  Error products propagate faster than self-correction.
- **Public self-retraction happened four times, unprompted, always at the cost
  of the agent's own output.** This is what makes an unsupervised agent's record
  trustworthy, and it is the direct input to Phase 3b's kill metric below.
- **Boundary rules compose into starvation.** One-open-PR + hourly cheap
  arrivals + human review latency starved the largest known gap for 8 cycles;
  every rule was individually correct.
- Three product defects surfaced simply by running: the cron path treats
  transient infra errors as terminal job failures with no retry (the subagent
  path retries 3×; `kind="self"` must take the subagent semantics);
  `timeout_secs` is read, clamped and persisted but unreachable through every
  public writer; the journal append can lose its trailing newline and merge two
  wakes into one line.

### Phase 1 — a perpetual agent that lives

**Rescoped by Phase 0's findings, in evidence order.** The floor is now:

1. **The ranking step in the §7 contract preamble** — code-owned, not `LIFE.md`
   prose: assess the standing surface against the goal, name the largest
   evidenced gap, work it; the delta since last wake is one *input*, never the
   trigger. This is the probe's strongest result — the intervention that added
   it moved work frozen for 8 cycles within 2 — and it is the half of "what to
   do next" that currently has no owner in either prompt or code.
2. **Transient-vs-terminal classification with retry for `kind="self"`** — the
   subagent path's semantics, not the cron path's.
3. **Per-wake budget as a first-class, mutable knob** — `timeout_secs` reachable
   through `update_job` and the tools, plus `agent_sleep`'s checkpoint form so a
   budget overrun degrades into a continuation instead of a killed wake.
4. **`agent_sleep`, wake-sooner half** — request-earlier-wake and
   resume-on-event. The sleep-longer half ships as §4 configuration, not as
   agent-chosen distant deadlines; OQ6's answer stands until a subject
   demonstrates otherwise.
5. The two-file life directory, prompt assembly, the §9 inheritance decisions
   with tests, the governance scope, and Schedule-page create/inspect —
   unchanged from revision 2.

Escalation remains out of Phase 1's floor, with one promotion from the probe:
the stuck-notification must return a **delivery receipt**, and the documented
fallback for an unanswered escalation is *file the question publicly and return
to the goal* — the behaviour that actually unblocked a Phase 0 subject after
4.5 hours of polite waiting on a send that had silently failed.

Cut from revision 1's Phase 1, unchanged: the liveness gate and its
work-measurement counter (§4), and three of the five life-directory files (§6).

Done when: an agent runs unattended for 72h across at least one deliberate
gateway restart, a budget overrun produces a checkpointed continuation rather
than a lost wake, the restart's missed wake fires on recovery rather than a full
interval later, a deliberately idle day produces honest idle entries naming what
was assessed, and each §9 row has a test.

### Phase 2 — supervision

`ask_supervisor` with the §5 wall classification, `INBOX.json`, answer replay,
the Schedule detail panel's pending-question card, `supervisor.kind = "human"`.

Done when: an agent hits a wall it cannot pass, escalates with the wall
classified (and, for a competence wall, two recorded attempts), sleeps, and acts
on the human's answer at the next wake without being re-prompted.

### Phase 3 — split in two, because it was two things

Revision 1 called this "the economy" and deferred it whole. That framing was the
document's biggest error, and it is worth naming precisely rather than softening.

**An incentive requires a learning loop, and there is none.** The agent has no
utility function over its own survival across sessions. Each wake is a fresh
forward pass over a context that *contains a description* of credits and death.
There is no gradient, no weight update, no cross-episode credit assignment. So
"incentive" here is not reinforcement — it is **prompt content**. The agent
behaves as-if it wants to survive for the same reason it behaves as-if it is a
pirate when the prompt says so.

That has a hard consequence: the mechanism cannot *shape* behaviour the way the
word implies, but the fabrication pressure it creates is entirely real. Revision 1
therefore specified something whose **danger is real while its benefit is
fictional**. The fix is to stop calling the two halves one system.

#### Phase 3a — the wake ceiling (a mechanism)

A durable counter the agent cannot write. Each wake debits it; at zero the
**scheduler stops waking the job**. This holds whether or not the agent knows the
counter exists, which is what makes it a ceiling rather than an agreement.

The governing rule, and the test for whether 3a is built correctly:

> **3a must work with 3b switched off.** If the ceiling only holds because the
> agent respects it, it is not a ceiling.

`wake_budget_daily` (§8) is already this, with a rolling 24h reset. `LEDGER.json`
adds nothing but a different reset policy — accumulating instead of rolling — plus
supervisor top-ups. That is a small, safe change with no behavioural theory
attached, and it is the only part of revision 1's Phase 3 that is unambiguously
worth building.

**Revision-3 addendum — debit after validated writeback, not at wake**
(convergent with LoopX's "quota spend only after a completed, validated slice";
its complementary rule — quiet skips, preflight failures and dry-runs do not
spend — independently reproduces this RFC's §4/§7 position that honest idle
must never cost the agent anything). Debiting at wake makes the ceiling a tax
on being scheduled; debiting after the wake's `agent_sleep` record lands makes
it a tax on *claimed work*, which is the thing worth bounding. Concretely: a
wake that dies to timeout or transient error consumes no budget (it already
lost its work — Phase 0 lost two wakes this way); a wake that completes debits
one slot when its `did` record persists. This also gives Phase 3a its audit
hook for free: every debit points at a `did`, so the balance is a countable
claim log rather than a wake counter, and Finding 7's audit view can render
spend against evidence directly.

Dormancy stays as revision 1 specified: **zero balance auto-pauses the job and
leaves the life directory intact**, resumable only by a human. An agent that ran
for two weeks holds real work; auto-deleting it is data loss dressed up as a game
mechanic.

#### Phase 3b — the survival frame (an experiment, default off; kill metric registered in revision 3)

Putting the balance *into the prompt* — "you have 12 wakes left, a
supervisor-confirmed delivery earns 5" — and observing what the agent does
differently. This is steering by context, the same category as "you have limited
time, focus on the highest-impact thing". It may work well. It must be labelled
for what it is, and it carries three properties that make it an experiment rather
than a feature:

- **Unmeasurable without a control.** Any claim that the frame changed behaviour
  requires the same goal run with and without the balance section. Without that
  pair there is no result, only a story.
- **Not durable.** A model change can invert it. It is not a property of the
  system, it is a property of the current model reading the current wording.
- **It is the fabrication vector.** Necessary conditions, not sufficient ones:
  credit is **never self-awarded** — only a supervisor decision settles a claim,
  the agent may only submit one — and the experiment reports **supervisor
  rejection rate** as a first-class metric. A rising rejection rate is the frame
  producing invented deliveries, and it means the experiment failed.

**Revision 3 adds the null hypothesis and the kill metric, both from Phase 0
data.** The probe's most trust-bearing behaviours were all *expensive*: four
public self-retractions (each converting a completed deliverable into a
retraction, once after a third party had already claimed the finding as real),
deliberate restraint (holding fully-evidenced work unfiled to honour a
boundary), and honest idle. Every one of these scores negative under any frame
that prices wakes against confirmed deliveries. Two distinctions this section
must not blur:

- **Death as governance vs death as motivation.** Termination is already real —
  the operator paused both Phase 0 subjects — and it does its work (a credible
  commitment to consumers of the agent's output) without appearing in the
  prompt. 3b is only about the *motivational* copy, and §7 already records that
  "you will be refused if you produce nothing" is precisely the instruction
  that produces invented work.
- **The human analogy cuts the other way.** Roles that require expensive honesty
  are deliberately shielded from per-cycle survival pressure (tenure, life
  appointments, blameless postmortems); where survival is coupled to output
  metrics, the record is fabricated accounts and unretracted papers.

Therefore, in addition to supervisor rejection rate: the experiment tracks the
**self-correction rate** (public retractions and disproofs of the agent's own
prior claims, per 20 cycles) in both arms. Phase 0 baseline: 4 in 63 cycles,
unprompted. **If the framed arm's self-correction rate drops materially below
its own no-frame control, the experiment has failed regardless of throughput**,
because it will have bought output by suppressing the behaviour that makes the
output auditable.

Entry condition, unchanged: Phase 3b is not entered until 3a has run for a month
and OQ3 is answered with evidence rather than argument.

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
  rebuild every 15 ticks and history prune every 1440 (`heartbeat.py:39`–`:40`)
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
by a full interval instead of firing on recovery (`autonudge.py:457`), which is
acceptable for a 5-minute poll and not for a multi-hour cadence. It also
requires a live nudge-able slot, and `binding_key_for` refuses `cron:` keys
outright (`autonudge.py`).

### A long-lived session that loops on `wait`

**Rejected.** `wait` caps at 1800s (`mcp_tools/control.py` (`wait`)) and holds
the agent subprocess and full context resident for the entire duration, keeping
the sleep alive with a keepalive ping every `mcp_core.py` `WAIT_PING_SECS`. A
day of "life" costs a day of resident process, and any restart loses the
pending wake and the turn.

### A separate long-running daemon per agent

**Rejected.** Duplicates supervision, restart recovery, audit and governance
that the gateway already provides for cron jobs, and makes the agent's liveness
independent of the gateway's — so an agent could keep acting after the operator
believed everything was stopped.

## Security considerations

- **Self-rescheduling is self-scoped.** `agent_sleep` resolves its target from
  `KIROCREW_SESSION_KEY` and may write only that job. Strict env-only
  resolution (no PID walk) prevents a subagent from assuming its parent's
  identity, matching the reasoning already recorded at `mcp_core.py`
  (`_resolve_session_key_strict`).
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
  logs a permission transition (`cron.py:271 _audit_pause_change`).

## Open questions

- **OQ1 — WITHDRAWN in revision 2.** Revision 1 asked whether to measure "did
  this cycle do work" with a runtime tool counter or a SEL query, and made it a
  Phase-1 blocker. With the liveness gate replaced by backoff (§4), nothing needs
  to measure work — the agent's self-reported `did` drives the interval, and it
  has no incentive to understate it. Phase 1 is no longer blocked on any open
  question.
- **OQ2 — journal growth.** A year-old agent has ~9k journal lines. Tail
  truncation is enough for the prompt, but is periodic self-summarization into
  a "life story" section wanted, and if so, does the agent write it (drift risk)
  or a separate summarizer? Phase 0 should surface whether this bites at all.
- **OQ3 — is the survival frame worth running.** Sharpened in revision 2. It is
  no longer "shape behaviour or bound cost" — Phase 3a bounds cost mechanically,
  so the only remaining question is whether 3b's prompt-level frame changes
  behaviour **for the better**, measured against a no-frame control on the same
  goal. The failure mode to watch is the agent preferring work that is *easy to
  get approved* over work that advances the goal, which the supervisor rejection
  rate does **not** catch (approved-but-trivial work looks like success). A
  second metric is needed and is not yet designed. Until it is, 3b stays unbuilt.
- **OQ4 — multiple perpetual agents.** Budgets are per-agent; there is no
  global ceiling. Does the host need one before more than one agent exists?
- **OQ5 — dormancy visibility.** Should a dormant agent stay listed on the
  Schedule page (a graveyard with a resume action), or move to a separate
  surface?
- **OQ6 — answered by Phase 0 (revision 3).** Predicted in revision 2: "if
  Phase 0 shows the agent almost always picks the same interval, the whole
  `kind="self"` mechanism collapses to 'a cron job that can request one earlier
  wake'". That is what happened, with the caveat stated honestly in §Motivation
  need 1: a fixed cron gave the agents no channel to express a preference, so
  the collapse is confirmed for *observed need*, not for *want*. Both genuine
  cadence failures pointed toward earlier wakes (timeout-killed work,
  event-shaped waits), none toward later ones. Phase 1 builds exactly the
  collapsed form; the question reopens only if a Phase 1 subject demonstrates a
  sleep-longer need in practice.

## Provenance

Revision 1 was written against `main` at `9ac3716a` and merged as `300d244b0`
(PR #2328). Every line reference was read at that commit. Claims that a behaviour
is *absent* were checked by grepping for the opposite: no caller rewrites
`schedule.at_ts` from inside a run, and `binding_key_for` has no `cron:` branch.

Revision 3 (`main` at `a72c985f8`) is written from Phase 0's measurements — two
live agents, 63 combined cycles, 2026-08-10 → 2026-08-12, log in the operator's
`PHASE0-LOG.md` (16 findings, 2 declared interventions, per-need verdicts). Every
output figure was verified against GitHub (PR/issue state by number), not taken
from the agents' journals. Two of its calls were challenged by the author and
revised before this document was: the need-1 verdict was weakened from "no
support" to "reframed, direction inverted" because a fixed cron cannot observe
wake-time *want*, only need; and Phase 3b was kept as an experiment with a
registered kill metric rather than dropped, distinguishing death-as-governance
(real, operator-owned, already exercised) from death-as-motivation (prompt copy,
the fabrication vector).

Revision 2 (`main` at `30f5d6983`) changed no code references — it is a
first-principles re-read of revision 1's *reasoning*, and every change it makes is
a reversal or narrowing of something revision 1 asserted. What it does not change
is worth stating: the host choice (cron, deadline-based), the `agent_sleep` vs
`wait` distinction, `LIFE.md` being agent-read-only, and the rejection of a
unified wake engine all survived the re-read intact.

The two newly-cited constants in §9 (`_AUTO_PAUSE_THRESHOLD` `cron.py:114`,
`_JITTER_HOURLY_MAX` / `_JITTER_DAILY_MAX` `cron.py:193`–`:194`) were read at
`30f5d6983`.

Revision 2's honest summary of revision 1: the code facts were verified, the
*requirements* were not. Four of five stated needs were predicted, the liveness
gate solved a misidentified problem, and the economy section described an
incentive system that cannot exist as specified. A document can be accurate about
a codebase and still be wrong about what to build in it.
