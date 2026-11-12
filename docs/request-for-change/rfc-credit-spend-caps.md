---
title: Attribute subagent credits to their session, then cap an unattended loop at a turn boundary
status: draft
author: javenciu
created: 2026-11-11
last-audited: 2026-11-11
audited-at: fd94aa555d
doc-pr:
implementation-prs: []
tracking-issues: [6338]
supersedes: []
superseded-by: []
---

# RFC: Attribute subagent credits to their session, then cap an unattended loop at a turn boundary

Status: draft, 2026-11-11. Nothing is built and this document changes no code. Code facts are measured at `fd94aa555d`; section 11 lists them. The decisions a maintainer needs to sign are in section 10. Tracks [#6338](https://github.com/kirodotdev/KiroCrew/issues/6338), which the issue triage pointed at this process, and [#21973](https://github.com/kirodotdev/KiroCrew/issues/21973), which is its first step.

## Summary

Two steps, in this order.

1. **PR 1: attribution, no flag.** A subagent's usage row names the session that spawned it. The two readers that build session totals, `cost_breakdown` and `slot_spend`, add a child's credits to that session. Today they drop the row.
2. **PR 2: the cap, off by default.** A new setting, `budget.session_credits` (Kiro credits, `0` or absent means no cap), stops a goal, monitor or other AutoNudge loop before its next cycle once its session and the subagents it started have spent that many credits. The check runs where the loop is about to start a turn. A turn or a subagent that is already running is never cut: the cap drains. Typed messages are not refused (open question 7).

Time-window limits, a warning level, spawn refusal, channel loops and per-cron limits are later PRs. Each has its own open question.

## Motivation

### Current state on main

- A kiro turn's credits are metered. `parse_metadata` (`src/kiro_crew/acp/_dispatch.py`) and the usage tracker (`src/kiro_crew/acp/client_usage.py`) each sum the `meteringUsage` entries whose unit is `credit`. The tracker adds the sum to the turn's stats.
- Every dispatch surface records a turn's billing in one function, `persist_token_record_async` (`src/kiro_crew/dashboard/handlers/usage.py`). It builds the row with `_build_token_record`, emits the turn histogram and appends the row on a worker thread. The dashboard, cron, subagent, background and workflow paths all call it (section 11).
- A subagent run writes one row per prompt it answers, when that prompt ends (`_run_inner_impl`, `src/kiro_crew/subagent_manager/run_turn.py`). The row is built by `persist_usage_row` (`src/kiro_crew/subagent_manager/usage_row.py`) with `surface="subagent"` and the subagent's own key. It holds no key of its parent.
- The run already knows its parent. `SubagentInfo` carries `parent_session_key` and `root_session_key` (`src/kiro_crew/subagent_parts/run_record.py`). Spawn admission sets both: a nested spawn takes its parent record's root (`spawn_impl` in `src/kiro_crew/subagent_manager/admission/gate.py`), and a retry, a drained row or a continuation carries the root it had (`src/kiro_crew/subagent_manager/continuation.py`). The cancel path already finds a session's running children by either key, and its queued-stop report builds a `SubagentInfo` from the same `parent_session_key` (`src/kiro_crew/subagent_manager/cancellation.py`). Nothing in the subagent code knows an AutoNudge loop id (witness W4).
- The readers drop the row. `cost_breakdown` (`src/kiro_crew/dashboard/handlers/usage_cost.py`) and `slot_spend` (`src/kiro_crew/dashboard/handlers/usage_spend.py`) skip any slot for which `is_session_slot` is false, and `_NON_SESSION_CHANNELS` in `usage_store.py` is `{"subagent"}`. The comment there gives the reason: the row "cannot be attributed". `test/metrics/test_cost_breakdown.py` pins it (`test_a_subagent_is_not_a_session_and_reaches_no_figure`), and `docs/system-specs/modules/metrics.md` says the totals are those of the sessions the panel lists, not of the account.
- The Sessions table adds a running task's live spend to its owner's figure in the browser (`rollUpTaskCredits` in `website/src/pages/system/sessionRows.ts`, fed by `_RunCreditAccounting.live_credits` through `task_memory_rows_impl` in `src/kiro_crew/subagent_manager/monitoring.py`). That covers running tasks only, and the server holds no such figure.
- A per-session total exists only as a read of the rows. `slot_spend` sums them over a 7-day window and caches the result for 60 seconds. Its only caller is `SessionMemorySampler`, which builds the Sessions table rows (`src/kiro_crew/dashboard/session_memory.py`).
- Nothing compares a credit figure with a limit before a turn starts. `git grep` for the usual names of a cap setting returns nothing in Python source (witness W1), and the loop service, both nudge fire paths, the cron fire path and the dispatch gates contain no such comparison (witness W2).
- A loop is bounded by its cycle count and an optional wall-clock budget, both checked by the loop service before it fires (`src/kiro_crew/autonudge_service/firing.py`). A turn in flight is bounded by time only: `agent.chat_turn_timeout_secs`, default 4 hours (`src/kiro_crew/config/sections_agent.py`).
- The structured-monitor token budget does not help a kiro session. Kiro reports no tokens, and the monitor seam reads zero as "unavailable" (`src/kiro_crew/monitoring/completion.py`).

### The problem, in the thread's own numbers

- On #6338, a user reports a goal loop that wrote a 5,000-credit budget into its own loop file and spent about 31,000 credits in two and a half days. About 85% of that was in subagents the loop dispatched. Nothing stopped it.
- On #21973, the same user measured 17 days on one install: subagents were 69% of all credits (144k of 208k), and none of it reached a session total. On one day, 112 of 113 subagent slots in the usage log had exactly one row, written when the run ended, so a run of 30 to 138 minutes was invisible until it finished. Mapping runs back to a parent took a scrape of spawn receipts, and 76 of 978 runs stayed ambiguous.

### Why attribution comes first

A cap that counts only a session's own turns would have missed the loop in the thread. By the thread's numbers, 15% of 31,000 is about 4,650 credits, which is under the loop's own 5,000 budget. The cap would never have tripped. The same user lists, as the first requirement of any cap, that it must include the subagents the session dispatched, and says this depends on #21973.

So the order is fixed by the data. PR 1 makes the child spend a number the server holds, and it is useful alone: it fixes the totals in the Sessions table and the Spend view, which today leave out the largest block of spend. PR 2 adds the cap on top of a total that is right.

## Goals

- A child's credits reach its session's total, in the two places the server builds one.
- One setting that stops an unattended loop from starting another cycle once its session and its children have spent a stated number of credits.
- The check runs only between turns. A running turn or a running subagent is never cancelled by it.
- A stopped loop says why, says what was spent, and says how to continue.
- With the setting at its default, every decision matches main.
- Each PR is small, independently mergeable and independently abandonable.

## Non-goals

- Cancelling a turn or a subagent that is running.
- Holding a person's typed message. See open question 7.
- Converting dollars or tokens into credits. See open question 11.
- Time-window limits, a warning level, spawn refusal, per-cron and channel-loop limits. They are later PRs (section 8).
- A cap in credits per completed item. See section 8, "Not planned".
- Changing a default. Turning the cap on by default is for maintainers, later.

## 1. Every path that starts a turn, and where a check could refuse

| # | Path | Function where a check could refuse | In this RFC |
|---|---|---|---|
| 1 | A typed message in the dashboard | `_turn_dispatch_gates` in `src/kiro_crew/dashboard/chat_turn/turn_dispatch_gates.py`, beside the Ultra Mode pause check (`crew_pause.holds_slot`) | Never refused. Open question 7. |
| 2 | A queued message drained after a turn | the same gate; the drain calls `_run_chat` in `chat_turn/queue_drain.py` | Never refused. |
| 3 | A goal, monitor or legacy AutoNudge cycle | `_fire_dashboard_nudge` in `src/kiro_crew/slack/gateway_runtime/nudge_dashboard_fire.py`, after its `crew_pause.holds_slot` check. The turn itself is started by `_start_dashboard_wake_turn` | **PR 2.** The loop stops. |
| 4 | An AutoNudge cycle bound to a channel session | `_fire_slack_nudge` (`slack/gateway_runtime/nudge_channel_fire.py`), `_fire_dm_nudge`, `_fire_discord_nudge`, `_fire_webex_nudge`. They do not call `_run_chat`. | Later PR. The channel session's key must be mapped to the total first. |
| 5 | A cron job's own run | `_cron_callback` in `slack/gateway_runtime/cron_fire.py`, after the message-job fire-time gate (`_await_cron_fire_time_gate`). It runs its own turn, not `_run_chat` | Later PR: per-cron `max_credits`. |
| 6 | A cron result delivered into a dashboard session | queued as kind `CRON_NOTIFICATION_KIND`, actor `cron` (`chat_turn/steer_queue.py`) | Never refused. It carries a result the session needs. |
| 7 | A subagent's own prompts | `_run_inner_impl` in `subagent_manager/run_turn.py` (the prompt loop) | Never refused. PR 1 attributes their rows; PR 2 counts them. |
| 8 | A new subagent dispatch (`spawn_run`) | `spawn_impl` in `subagent_manager/admission/gate.py` | Later PR: spawn refusal. Open question 9. |
| 9 | A subagent completion woken into its parent | queue kind `SUBAGENT_COMPLETION_KIND`, actor `subagent` (`steer_queue.py`) | Never refused. Refusing it would drop a result. |
| 10 | Agent-started, task-runner, app and gateway turns | `_run_chat` with `_turn_actor` of `agent`, `gateway` or `app` (`chat_runner.py`, `turn_entry.py`) | Never refused. |

Rows 1, 2, 3, 6, 9 and 10 end in `_run_chat` and so reach `_turn_dispatch_gates`, which already knows how to refuse a turn. Rows 4, 5, 7 and 8 run their own turns or their own admission, each with its own function. PR 2 refuses at row 3 only. Why it does not refuse at the gate for rows 6, 9 and 10 is in section 4.4.

## 2. Prior art

Behaviors only.

- **Spending limits in model gateways.** Several gateways let an operator set a budget per key and per time window. A request over the budget is refused, and many send an alert before the limit. The unit is money or tokens. The key is the closest match to a session.
- **Per-run budgets in agent tools.** Some agent runtimes accept a maximum spend for one run and stop at the limit. Sub-agent spend counts toward the parent's, and a new sub-agent is refused once the limit is reached. This is why attribution comes before the cap.
- **Tracing tools.** Some tools record a parent and child path on every record, so spend rolls up to the parent. PR 1 stamps the same two facts.
- **Existing Kiro Crew work.** [#21973](https://github.com/kirodotdev/KiroCrew/issues/21973) is PR 1. [#32395](https://github.com/kirodotdev/KiroCrew/pull/32395) put running tasks' spend in the Sessions table, in the browser. [#24537](https://github.com/kirodotdev/KiroCrew/issues/24537) asks that `monitor_start` validate a loop's armed budget, and says "once #6338 gives the monitor a budget field". [#24538](https://github.com/kirodotdev/KiroCrew/issues/24538) adds cost rules to the loop skills. [#20552](https://github.com/kirodotdev/KiroCrew/issues/20552) reports an idle crew loop spending credits. [#35249](https://github.com/kirodotdev/KiroCrew/issues/35249) reports that the usage view shows nothing for a backend that bills in dollars. This RFC supplies the budget that #24537 is waiting for. It duplicates none of them. The earlier [Autopilot stage budgets](rfc-autopilot-stage-budgets.md) RFC bounded stage time, was superseded before acceptance, and proposed no credit cap.

## 3. PR 1: attribute a subagent's credits to its session

### 3.1 Stamp the row

`persist_usage_row` already holds the run record (`info`). It passes three more values to `persist_token_record_async`, which passes them to `_build_token_record`:

- `parent_session_key`: the session or subagent that spawned the run.
- `root_session_key`: the top session of the run's lineage. A child of a session has the same value as its parent key. A nested run has the top session, not the intermediate subagent, so a reader never walks a chain. If the parent's record is already gone when a nested run is admitted, admission falls back to the parent's own key. That is not a session slot, so a reader drops the row, as today.
- `loop_id`: the AutoNudge loop that was active on the root session when the run was admitted. Subagent code knows no loop id today (witness W4). Admission can read it with `AutoNudgeService.get_by_slot` (`src/kiro_crew/autonudge.py`) and keep it on the run record. It is best-effort: a loop re-armed after the run started is not the one that dispatched it. Nothing in this RFC's cap depends on it, because the cap sums per session. It exists so a later reader can sum spend per loop, as the thread asks.

Each key is written only when it is not empty, as the row's other additive keys are, so rows written before the change have no such keys and every reader already tolerates that. The value is a session key, not content, in a file that is already owner-only.

### 3.2 Read the row

One helper next to `is_session_slot` in `src/kiro_crew/dashboard/handlers/usage_store.py` answers "which session does this row belong to?". For an ordinary row it is the slot. For a subagent row that carries a `root_session_key` (else a `parent_session_key`) naming a session slot, it is that session. For any other row it answers nothing, and the row is dropped as today.

Both readers call it where they now call `is_session_slot`:

- `cost_breakdown` files the row under the owning session. The window total, the by-model, by-channel and by-category views and the conversation list then include the child's credits. The model stays the child's model, because that is where the money went. Each conversation gains `subagent_credits`, the part of its `credits` that came from children, so turn spend and delegated spend stay distinguishable.
- `slot_spend` does the same and returns `subagent_credits` beside `credits` and `turns`.
- In both, `credits` is the session's total including its children, and `subagent_credits` is the part that came from children. A child row adds credits and no turns. A turn is a turn of the session.

Rows with no stamp, and rows whose stamped session is not a session slot, stay dropped. Nothing is migrated and history reads as before.

### 3.3 Keep the Sessions table right

The table already adds a running task's figure, settled plus in-flight, to its owner (see the current-state list in the Motivation). Once the server's session figure includes a child's settled prompts, adding the whole live figure again would count them twice. PR 1 therefore sends each live task's settled part (`credits_settled`, from `SubagentInfo.credits`) with its payload, and `rollUpTaskCredits` adds only the in-flight part (`credits` minus `credits_settled`). A finished task's spend, which vanishes from the table today when the task ends, then stays in its owner's figure.

### 3.4 What does not change

- When rows are written: still one per prompt, when the prompt ends. The thread asks for a row per turn while a run is going. A run that answers a single prompt for two hours cannot write a row sooner without a mid-prompt flush, which is a different change. The cap reads the live in-memory figure of a running child instead (section 4.2). Open question 3 asks whether partial rows are wanted later.
- No flag. The change adds fields and fills numbers that were missing. Open question 1 asks maintainers to confirm that the totals should change.

## 4. PR 2: the cap on autonomous loop cycles

### 4.1 The setting

`budget.session_credits` is a float in Kiro credits. `0` or an absent key means no cap. It is the first key of a new top-level `budget` section. The section is declared now because a shipped key name is a one-way door, and the design note on #6338 already named the family: `budget.daily_credits` and `budget.warn_pct` are reserved for later PRs and are not declared by PR 2. Open question 5 asks maintainers to confirm the section, with an `agent.` key as the alternative.

A section is more work than a key. It needs a dataclass, its wiring through the config loader and its list of known sections, a row in the configuration reference, and tests, where an `agent.` field is one field and one read (`work_ledger` shows the wiring: its dataclass in `config/service_sections.py`, its field and `to_dict` entry in `loader_config.py`, its build in `loader_pipeline.py` and `section_builders.py`, and its name in `config/resolution.py`). PR 2 ships the section together with the check that reads it, so the key never lands with nothing reading it.

The guard reads the value from the live snapshot (`config/live.py`) on each check, so a raised cap takes effect on the next fire without a restart. When no snapshot exists, or the read raises, the guard treats the cap as off and logs a warning. Fail-open is deliberate: the meter is best-effort analytics, and a failed read must not stop every loop on the machine. The cycle cap and the turn ceiling still bound spend.

The setting is not added to the config-card editable set, so an agent cannot propose its own cap through a card. A Settings row is a later PR.

### 4.2 The running total

A new module `src/kiro_crew/spend_guard.py` holds a bounded dict from session key to two floats: credits and dollars. The key is built with `spend_key_for_slot` (`usage_spend.py`), the rule the Sessions table already uses.

- **Feed.** `persist_token_record_async` calls `spend_guard.note_turn(row)` right after the row is built and before the file write. For an ordinary row the key is its slot. For a subagent row it is the row's `root_session_key`. The call does no I/O and does not raise. Feeding the total there, and not in the dashboard turn code, means a turn that ended abnormally (turn ceiling, Stop, sign-out) is counted too, because that path calls the same function, and so is every child, because PR 1 stamps its row.
- **Seed.** The first check for a key in a process seeds the total from `slot_spend()` for that key, read on a worker thread because it re-reads shards. After PR 1 that figure includes children. After the seed, the total moves only by `note_turn`. A turn noted before the seed is kept and added to it. The race can count one turn twice and never counts a turn zero times, so it errs toward stopping early. The seed uses the same 7-day window as the Sessions table, so the figure in the stop notice matches the Credits column the person can see (open question 4).
- **Running children.** A child's row lands when its prompt ends, so a long worker is invisible to the ledger while it runs. The check therefore adds the in-flight credits of the runs under the key: for each run that is neither done nor queued and whose `root_session_key` or `parent_session_key` is the key, `live_credits()` minus the run's settled `credits` (`_RunCreditAccounting` in `src/kiro_crew/subagent_parts/run_output.py`). The in-flight part only, so a settled prompt is not counted by both the ledger and the live figure. The read is synchronous, like the sweep in `cancellation.py`, and the dashboard state holds the manager (`state.subagents`, as in `slack/gateway_runtime/subagent_injection.py`). A prompt that has just settled and whose row is not yet written can be missing for a moment. The error is at most that one prompt.

### 4.3 The check

In `_fire_dashboard_nudge`, right after the pause check, the fire asks `spend_guard.over_cap(slot, subagents)`. It is true when the cap is above `0` and the session's total is at or above it. When true:

1. The loop is stopped with a new stored reason, `credit_cap`, through `autonudge_svc.update(loop.id, active=False, stopped_reason=CREDIT_CAP_REASON, expected_generation=loop.config_generation)`. This is the call the model-refused guard already makes in the same function. The generation fence means a loop re-armed with new text during the check is not stopped by the old verdict.
2. The owner is told through `_notify_nudge_expired` (`slack/gateway_runtime/nudge_wiring.py`), which gets a new branch beside the model-refused one.
3. The fire returns `UNAVAILABLE`, as the other guards do. No nudge row is appended and no cycle is spent, because the service counts only dispatched cycles.

`CREDIT_CAP_REASON` joins `_TERMINAL_BOUND_REASONS` (`autonudge_service/model.py`). That makes it re-armable, and a later reasonless pause cannot overwrite it. The popover shows the bare word "Paused" for a reason it has no label for (`website/src/components/AutoNudgePopover.tsx`), so PR 2 needs no website change.

### 4.4 At the cap: drain, do not hard-stop

A hard stop strands half-finished work: a stack of dependent drafts where the base is not done. The thread asks for a drain, and so does this design.

- A turn that is running finishes. The total can pass the cap by what that turn spends and by what the running subagents spend before they finish.
- Subagents that are running finish and record. None is cancelled. Their later rows still reach the total, so the figure in the next notice is true.
- No new autonomous cycle starts. The fire stops the loop and says so.
- A typed message always runs. It is how a person continues once without changing anything. The loop stays stopped.
- The loop stays stopped until the owner restarts it from the goal popover. Raising the cap does not restart it (open question 6). Restarting without raising the cap stops it again at the first fire and spends no cycle.
- The loop is never retried on a timer after a cap stop. It is inactive, not busy.

Rows 6, 9 and 10 of section 1 are not refused because each is a single delivery caused by work that already ran: refusing a subagent completion would strand the result its parent is waiting for. One gap remains, and it is named here on purpose. A child's completion wakes its parent session, and the turn it starts may dispatch new children, with no loop cycle involved. PR 2 does not stop that. PR 3, the next PR in the plan, refuses a new dispatch from a session over its cap (open question 9), so the cap is not announced as a stop for the runaway in #6338 until PR 3 lands.

The notice reads, in substance: "Loop stopped. This session and the subagents it started have used N credits, against its cap of M (budget.session_credits). Turns and subagents already running will finish and still count. Raise the cap, then restart the loop from the goal popover." N is the 7-day total, and the notice says so.

### 4.5 A backend that reports no credits

The total is credits only. A backend that bills in dollars and tokens leaves `credits` at `0` on every turn, so its total stays `0` and the cap never trips. The module also adds up dollars. When the cap is set and a session has recorded dollars and no credits, the guard logs one warning per loop: "cap not enforced, this session bills in dollars". The help text says the same. A dollar cap is open question 11.

### 4.6 Failure modes

- Gateway restart: the total is seeded again from the shards on the first check. A loop restored after a restart is checked before its first cycle. A child that was running at the restart has no live figure and its unfinished prompt has no row, so that part is lost with the run.
- Unreadable shards: the seed is `0` and a warning is logged. The cap then counts from the restart. This is the fail-open choice in 4.1.
- Memory: the dict is bounded and drops its oldest key past the bound. A dropped key seeds again.

## 5. Trust boundary

PR 1 adds a field to rows and moves numbers in two readers. It reads and writes nothing new outside the usage store.

The cap can only stop a loop. No input of PR 2 starts a turn that would not start on main. A wrong total can stop a loop early or late by at most the error in the meter. The cap is not a security boundary against a model that spends in one turn what it likes: a running turn is limited by time only. It is a guard against unattended repetition.

## 6. Alternatives considered

- **A. Ship the cap first, counting only the session's own turns.** Rejected by the numbers in the Motivation: it would not have stopped the loop in the thread.
- **B. Hold every turn, typed ones included.** The check goes in `_turn_dispatch_gates` for all actors. It covers more paths with one change, but a person who cannot send a message to a stuck session has lost the way to fix it. The pause check shows how much bookkeeping a held turn needs (`owe_turn` in `src/kiro_crew/crew_pause.py`). Open question 7.
- **C. Hard stop at the cap: cancel running turns and subagents.** Rejected: it strands half-finished work, and the reporter in the thread accepts a stop at the turn boundary.
- **D. Check in the loop service.** The service knows cycle counts, not slots or credits (`firing.py`). Giving it a credit reader couples it to the dashboard store. The dashboard fire already has the slot and the stop-and-notify pattern.
- **E. Read `slot_spend` on each check.** No new module and no feed. The figure is a 7-day window, re-read from shards, cached for 60 seconds. A gate that can be 60 seconds stale, and that costs a shard read, is a poor fit for a check that runs every cycle. The in-memory total is seeded from it once.
- **F. A script cron that sums the usage log and writes a stop file.** The thread tried this. It cannot see a running subagent and cannot tell which loop a subagent belongs to. PR 1 and the live figure in 4.2 are what it lacks.
- **G. Key it as `agent.session_credit_cap`.** Smaller now. A key name cannot be taken back once users set it, and the later window and warning keys would then sit in another place. Open question 5.

## 7. Backward compatibility and rollout

- PR 1 adds optional row keys. Old readers ignore them. New readers read old rows as before, because a row with no stamp is dropped as today.
- PR 1 changes numbers users can see: the Sessions table, the Spend view's totals and its by-model, by-channel and by-category splits. They go up by the child spend that was missing. The comment in `usage_store.py`, the test that pins the old rule and the sentence in `docs/system-specs/modules/metrics.md` change in the same PR.
- The `budget` key is new, so every existing config reads it as `0`. With `0`, `over_cap` returns false before it reads the ledger, and `note_turn` still runs but only adds two floats to a dict.
- An older build that reads a loop stopped with the new reason treats an unknown reason as a kept stop (the comment above `_REPLACEABLE_LOOP_STOP_REASONS` says it fails closed to preserved) and its popover shows a bare "Paused".
- No data migration. No default changes. The cap ships off and stays off until maintainers choose otherwise.

## 8. PR plan

Sizes are estimates, not measurements. Each PR is independently mergeable, and none turns the cap on.

| PR | What | Estimated size | Needs |
|---|---|---|---|
| 1 | Attribution (#21973): section 3 | about 150 added source lines in about 10 files | open questions 1 and 2 |
| 2 | The `budget` section with `session_credits`, the ledger, the check, the stop reason and the notice | about 255 lines in 13 source files, 7 of them a few lines of config wiring, plus the generated baseline, two docs pages and tests | PR 1; open question 5 |
| 3 | Spawn refusal from a session over its cap, which closes the gap named in section 4.4 | about 50 lines | PR 2; open question 9 |
| 4 | Settings row, popover label and notice strings | about 40 lines in about 4 files | PR 2 |
| 5 | One warning at `budget.warn_pct` (default 80), once per session, and a one-line notice on a typed turn | about 60 lines | PR 2; open questions 7 and 10 |
| 6 | Channel loops call the same predicate | about 80 lines | PR 2 |
| 7 | Per-cron `max_credits` | own amendment if the job schema needs a migration | PR 2; open question 10 |
| 8 | `budget.daily_credits`, a window across sessions and crons | not sized | open question 10 |

**PR 1 (about 150 lines, about 10 files).**

- `src/kiro_crew/dashboard/handlers/usage.py` (about 18): three keyword arguments and three row keys.
- `src/kiro_crew/subagent_manager/usage_row.py` (about 8): pass the stamp.
- `src/kiro_crew/subagent_parts/run_record.py` (about 3) and `src/kiro_crew/subagent_manager/admission/gate.py` (about 15): the loop id on the run record.
- `src/kiro_crew/dashboard/handlers/usage_store.py` (about 25): the owning-session helper and the comment.
- `src/kiro_crew/dashboard/handlers/usage_cost.py` (about 35) and `usage_spend.py` (about 25): use it.
- `src/kiro_crew/subagent_manager/monitoring.py` (about 3), `website/src/pages/system/sessionRows.ts` (about 12) and `website/src/api/client/sessions.ts` (about 2): the Sessions table.
- Tests and the metrics spec. If reviewers want it smaller, the loop id (two files) and the Sessions table (three files) split off as PR 1b without changing the rest.
- Exit criteria: a row written by `persist_usage_row` carries `parent_session_key` and `root_session_key`, and `loop_id` when the root has an active loop at admission; a nested run's row carries the top session as its root; with one session row of 10 credits and four child rows of 5 credits stamped to it, `cost_breakdown` reports a window total of 30, one conversation with `credits` 30, `subagent_credits` 20 and `turns` 1, and `slot_spend` reports the same two figures for that key; an unstamped subagent row is dropped as before; a running task's settled prompts are counted once in the Sessions table.

**PR 2 (about 255 lines, 13 source files).** The config section: A dataclass in a new `src/kiro_crew/config/sections_budget.py`, its export in `sections.py`, the field and `to_dict` entry in `loader_config.py`, the build in `loader_pipeline.py` and a builder, the import in `loader.py`, the name in `resolution.py`, plus the regenerated `config-baseline.json`, a row in the configuration reference, a line in the config system spec and tests. The guard: `src/kiro_crew/spend_guard.py` (new, about 110), `dashboard/handlers/usage.py` (about 8), `slack/gateway_runtime/nudge_dashboard_fire.py` (about 35), `autonudge_service/model.py` (about 10), `slack/gateway_runtime/nudge_wiring.py` (about 16), and a read-only helper on the subagent manager for the in-flight credits under a root key (about 15). Exit criteria: an absent key reads `0`; a negative, `NaN` or non-number reads `0`; the key is in the baseline with default `0`; a config without a `budget` section loads unchanged; with the key at `0`, a loop fires as on main; with the key at `5` and a total of own turns 2.0, settled child rows 2.0 and a running child's in-flight 1.0, a message loop and a structured-monitor loop each stop with `credit_cap` before a cycle is spent, and the notice names both figures; with in-flight 0.9 the loop fires; the running child is not cancelled and its later row still counts; a typed message in a capped session runs; a raised cap takes effect on the next fire and restarting the loop then lets it run; an unreadable snapshot fires and logs; a dollar-only session fires and logs once.

**Not planned.** A cap in credits per completed item. The thread points out that a per-loop number is a guess (the loop's was six times too low) and that credits per finished item is measurable across loops. It needs a definition of a completed item and a reader for it, and no code in this RFC assumes either.

## 9. Relation to the design note on #6338

The design note on #6338 (posted 2026-11-09) proposed the same direction with a wider first cap. This RFC keeps its shape and narrows it.

**Kept from the note.**

- Attribution first: stamp `parent_session_key` and the loop id on the usage row and roll the spend into the parent in `cost_breakdown`, about 150 lines, no flag. That is PR 1.
- Off by default. The note's flag defaults off; here the setting's `0` is the off state.
- Interactive turns are never refused. A running turn finishes.
- A sub-agent's spend counts against the session that spawned it.
- Fail open with a logged warning when the meter is unreadable.
- A backend that reports no credits is "not enforced", never counted as zero spend.

**Changed from the note, and why.**

- **PR 2 is narrowed to dashboard loop cycles.** The note's second PR was a guard module with admission for cron and autonudge. Spawn refusal becomes PR 3, right after the cap, and cron and window limits move to later PRs, each with its open question (9 and 10). The runaway in the thread is a loop, and one function fires a loop cycle, so the first cap is small enough to review. Cron fires through its own runner and failure alert. A spawn refusal is made in the subagent manager and has drain semantics. A window needs a reset rule and a total across sessions. Each is a different check point and a different decision.
- **No on/off switch.** The note's `budget.enabled` becomes `0` for each key. A cap that is set but switched off is a state nobody needs. Open question 5 lets maintainers bring a switch back.
- **The warning moves to a later PR**, so the stop can be reviewed alone. `budget.warn_pct` stays reserved.
- **Drain is stated outright**, with the thread's request that running subagents finish and record (section 4.4).
- **The cap is per session, not per loop.** The meter is keyed by session. PR 1 stamps the loop id so a later PR can sum per loop.

## 10. Open questions for maintainers

Each has a recommended answer, so a maintainer can sign them one at a time.

1. **Should the Sessions table and the Spend view include child credits?** This reverses the rule in `usage_store.py` and `docs/system-specs/modules/metrics.md`, which said totals are those of the sessions the panel lists. Recommended: yes. That rule existed because a subagent row could not be attributed and its money would sit in a total with no row to explain it. A stamped row has a row to explain it: its session's `subagent_credits`. The largest block of spend (69% in the thread's measurement) is missing from every total today. Unstamped history stays dropped. If the answer is no, PR 1 shrinks to the stamp and a `subagent_credits` field that the totals leave out, and PR 2 seeds from that field.
2. **Stamp `root_session_key` as well as `parent_session_key`?** Recommended: yes. The run record already holds it, a nested run then rolls up in one hop, and the readers never walk a chain. It adds one row key.
3. **Should a running subagent write partial usage rows?** The thread asks for it so a long worker is not invisible. Recommended: not in PR 1 or 2. The cap reads the live in-memory figure (section 4.2), which is what a partial row would carry. Revisit if the Spend view needs mid-run figures. A restart loses the unfinished prompt either way.
4. **Over what period is the session total counted?** Recommended: the same 7-day figure the Sessions table shows, so the number in the notice matches what the person sees. A lifetime total needs a stored counter on the session, which is a persisted format and a separate decision.
5. **Confirm the `budget` section, or key it as `agent.session_credit_cap`?** Recommended: the `budget` section. The shipped key name is a one-way door, the note already used `budget.*`, and the window and warning keys belong beside it. The cost is a section file, its wiring in six files, a configuration row and tests (about 65 of PR 2's lines, in 7 source files), against about 29 lines in 2 files for an `agent.` key. A rename later would need an alias. A related call: whether a global `budget.enabled` switch is wanted beside the zeros. Recommended: no.
6. **Does raising the cap restart the loops it stopped?** Recommended: no. A Settings edit should not start spending. The owner presses Play. If maintainers want it, it is a later PR that re-arms loops stopped by `credit_cap` when a subscription on the key sees the cap rise.
7. **Is a typed message also held when the cap is spent?** Recommended: no. A person at the screen can stop the work, and a held message removes the way to fix a stuck session. In PR 5, show one line in the session that the cap is spent.
8. **Should a cap on a structured monitor use the same stop?** Recommended: yes, since kiro turns add no tokens to the monitor's own token budget (`monitoring/completion.py`) and the credit cap is the only bound a monitor on kiro has. Unverified: whether `autonudge_svc.update` stops a monitor loop the way it stops a message loop. The existing guards are written for message loops only. PR 2's tests must cover both loop shapes. If the monitor needs its own stop call, PR 2 uses it.
9. **Should a session over its cap be refused new subagent dispatches?** Recommended: yes, as PR 3, right after the cap, at `spawn_impl`. Without it, a child's completion wake can start new children while the loop is stopped (section 4.4). The refusal should return a structured message that names the limit, and children already running finish.
10. **What comes after PR 2?** Recommended order: spawn refusal first (PR 3), because until it lands a completion wake can still start new children under a stopped loop; then the Settings row, the warning (`budget.warn_pct`, default 80, once), channel loops, the per-cron limit, and the time window last. A cron limit has one natural owner and one check point. A window needs a clock, a reset and a total across sessions.
11. **What unit?** Recommended: credits only. A session on a dollar-billing backend is named as not enforced. If wanted, add a separate dollar key later. Do not convert between units.
12. **Fail open or fail closed when the setting cannot be read?** Recommended: open, with a logged warning (section 4.1), as the meter is best-effort.

## 11. Audit table: code facts at `fd94aa555d`

Each row names the symbol and its file, so it survives a line move. The absence claims have a witness command under the table.

| Fact | Symbol and file |
|---|---|
| A kiro metadata frame's `credit` entries are summed | `parse_metadata` in `src/kiro_crew/acp/_dispatch.py`; the tracker in `src/kiro_crew/acp/client_usage.py` sums them again and adds the result to `last_prompt_stats.credits` |
| Kiro fills `credits`; the dollar-billing backends fill `cost_usd` and tokens | `TurnUsage` in `src/kiro_crew/acp/types.py` |
| One function records every surface's billing | `persist_token_record_async` in `src/kiro_crew/dashboard/handlers/usage.py`; the row is built by `_build_token_record` and written by `_write_token_record` |
| Cron, subagent, background and workflow turns call the same recorder | `src/kiro_crew/slack/gateway_runtime/cron_turn.py`; `persist_usage_row` in `src/kiro_crew/subagent_manager/usage_row.py`; `src/kiro_crew/llm_helpers_parts/background_turns.py`; `src/kiro_crew/workflows/agent_exec.py` |
| Abnormal turns record through the same function | `_persist_abnormal_turn_usage` in `src/kiro_crew/dashboard/chat_turn/turn_stats.py` |
| A subagent writes one row per prompt, when the prompt ends | the comment above `_persist_usage_row` in `_run_inner_impl`, `src/kiro_crew/subagent_manager/run_turn.py` |
| A run record holds its parent and root session keys | `SubagentInfo.parent_session_key` and `root_session_key` in `src/kiro_crew/subagent_parts/run_record.py`; set by `spawn_impl` in `src/kiro_crew/subagent_manager/admission/gate.py`; carried by `src/kiro_crew/subagent_manager/continuation.py` |
| Children of a session are found by either key | the running-children sweep in `src/kiro_crew/subagent_manager/cancellation.py` |
| A session total is a 7-day shard read with a 60 second cache | `slot_spend` and `_SLOT_SPEND_TTL_S` in `src/kiro_crew/dashboard/handlers/usage_spend.py`; `SPEND_WINDOW_DAYS` in `usage_store.py` |
| `slot_spend` has one caller | `SessionMemorySampler` in `src/kiro_crew/dashboard/session_memory.py` |
| The key rule for a session's spend | `spend_key_for_slot` in `usage_spend.py` |
| Subagent rows are dropped by both readers | `cost_breakdown` in `src/kiro_crew/dashboard/handlers/usage_cost.py`; `slot_spend`; `is_session_slot` and `_NON_SESSION_CHANNELS` in `usage_store.py` |
| The rule is pinned and documented | `test_a_subagent_is_not_a_session_and_reaches_no_figure` in `test/metrics/test_cost_breakdown.py`; `docs/system-specs/modules/metrics.md` |
| A running subagent's live credits, and how the table uses them | `_RunCreditAccounting.live_credits` in `src/kiro_crew/subagent_parts/run_output.py`; `task_memory_rows_impl` in `src/kiro_crew/subagent_manager/monitoring.py`; `rollUpTaskCredits` in `website/src/pages/system/sessionRows.ts` |
| The dashboard state holds the subagent manager | `_settle_collected_on_slot(slot, ids, state.subagents)` in `src/kiro_crew/slack/gateway_runtime/subagent_injection.py` |
| A loop is found by its slot | `AutoNudgeService.get_by_slot` in `src/kiro_crew/autonudge.py` |
| The dispatch gates refuse a turn while the pause holds the session | `_turn_dispatch_gates` in `src/kiro_crew/dashboard/chat_turn/turn_dispatch_gates.py`, reason `crew_paused`; `holds_slot` and `owe_turn` in `src/kiro_crew/crew_pause.py` |
| A turn's structural origin | `_crew_log_actor` in `src/kiro_crew/dashboard/chat_turn/turn_entry.py`; `ACTORS` in `src/kiro_crew/crew_log/emit_turn.py`; `_QUEUE_KIND_ACTORS` in `src/kiro_crew/dashboard/chat_turn/steer_queue.py` |
| A loop cycle fires in one function, with a pause check and guards that stop a loop | `_fire_dashboard_nudge` and `_start_dashboard_wake_turn` in `src/kiro_crew/slack/gateway_runtime/nudge_dashboard_fire.py` |
| The owner is told through one notification function | `_notify_nudge_expired` in `src/kiro_crew/slack/gateway_runtime/nudge_wiring.py` |
| A stop reason and its re-arm rule | `MODEL_REFUSED_REASON`, `_TERMINAL_BOUND_REASONS`, `_REPLACEABLE_LOOP_STOP_REASONS` in `src/kiro_crew/autonudge_service/model.py` |
| The loop service stops a loop at its cycle cap | the "Cycle cap reached" branch in `src/kiro_crew/autonudge_service/firing.py` |
| A cron job runs its own turn | `_cron_callback` in `src/kiro_crew/slack/gateway_runtime/cron_fire.py` |
| Spawn admission | `spawn_impl` in `src/kiro_crew/subagent_manager/admission/gate.py` |
| The popover shows a bare "Paused" for an unlisted reason | `PAUSED_STATUS_KEY` in `website/src/components/AutoNudgePopover.tsx` |
| A section is wired through the loader | `WorkLedgerConfig` in `src/kiro_crew/config/service_sections.py`; `KiroCrewConfig` in `loader_config.py`; `loader_pipeline.py`; `section_builders.py`; the list in `resolution.py` |
| Live config read | `snapshot` and `current` in `src/kiro_crew/config/live.py` |
| A kiro turn adds no tokens to the monitor budget | `src/kiro_crew/monitoring/completion.py` |
| No cap setting, no `budget` section and no credit comparison exist | witnesses W1 to W6 below |

### Witnesses for absence claims

Run from the repository root at `fd94aa555d`. `git grep` exits 1 when it finds nothing.

- **W1.** No Python file has a cap-style setting name: `git grep -nE "credit_cap|credits_cap|max_credits|session_credit|spend_cap|credit_limit|credit_budget|spend_limit|max_spend|cost_cap|daily_budget|monthly_budget" -- 'src/*.py'` prints nothing. The same pattern over all of `src` prints two lines, both in the pipeline-conductor skill text (`credit_budget_per_item`, advice the conductor reads, not a runtime check).
- **W2.** The loop service, both nudge fire modules, the notification wiring, the cron fire module, the dispatch gates, the dashboard turn admission module and the pause module: `git grep -nE "credit" -- src/kiro_crew/autonudge.py src/kiro_crew/autonudge_service src/kiro_crew/slack/gateway_runtime/nudge_dashboard_fire.py src/kiro_crew/slack/gateway_runtime/nudge_channel_fire.py src/kiro_crew/slack/gateway_runtime/nudge_wiring.py src/kiro_crew/slack/gateway_runtime/cron_fire.py src/kiro_crew/dashboard/chat_turn/turn_dispatch_gates.py src/kiro_crew/dashboard/chat_turn/turn_admission.py src/kiro_crew/crew_pause.py` prints 21 lines. Each uses the word as a verb (`credit_held_runtime`, "one allowance credit") or is the turn's own `_turn_credits` stat. None compares a figure with a limit.
- **W3.** Cron, the cron service, the spawn tool and the workflow package never mention credits: `git grep -nE "credits" -- src/kiro_crew/cron.py src/kiro_crew/cron_service src/kiro_crew/mcp_tools/spawn.py src/kiro_crew/workflows` prints nothing.
- **W4.** The usage-row path names no parent, and the subagent code names no loop: `git grep -nE "parent_session_key|root_session_key" -- src/kiro_crew/dashboard/handlers/usage.py src/kiro_crew/dashboard/handlers/usage_store.py src/kiro_crew/dashboard/handlers/usage_cost.py src/kiro_crew/dashboard/handlers/usage_spend.py src/kiro_crew/subagent_manager/usage_row.py` prints nothing, and `git grep -n "loop_id" -- src/kiro_crew/subagent_manager src/kiro_crew/subagent_parts src/kiro_crew/subagent.py src/kiro_crew/mcp_tools src/kiro_crew/mcp_core.py` prints nothing.
- **W5.** No per-session total is kept in memory: `git grep -n -i "credit" -- src/kiro_crew/dashboard/state_parts src/kiro_crew/dashboard/state.py` prints three comment lines in `slot_transcript.py` that use "credit" to mean "count toward". `slot_spend` and `daily_credits` have no caller that compares against a limit: `git grep -nE "slot_spend\(|daily_credits\(" -- src` prints the two definitions (`usage_spend.py`) and the callers `usage_sessions.py` and `session_memory.py`.
- **W6.** No `budget` section exists, and the key names are unused: `git grep -nE "budget\.(enabled|daily_credits|warn_pct|session_credits)" -- src docs website/src` prints nothing, and `grep -n '"path": "budget' config-baseline.json` prints nothing.
