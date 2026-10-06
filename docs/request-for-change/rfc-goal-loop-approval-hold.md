---
title: Goal loop approval hold -- an unanswered approval pauses a loop, and a work-ledger watch outlives its own bounds while work is open
status: in-progress
author: iamwhatever, with kirocrew-worker
created: 2026-10-05
last-audited: 2026-10-05
audited-at: a97659dfc3
doc-pr: null
implementation-prs: [16993]
tracking-issues: [16976]
supersedes: []
superseded-by: []
---

# RFC: Goal loop approval hold

- Status: in-progress. The decision was made by the operator who owns the conductor patrol (2026-10-05). The implementation is [#16993](https://github.com/kirodotdev/KiroCrew/pull/16993). That PR's First Principles lane reads an RFC's status off the base branch, so this document lands on its own first, as GOVERNANCE.md asks of an RFC. The implementation then rebases onto it.
- Amends [rfc-goal-popover-pause-play-controls.md](rfc-goal-popover-pause-play-controls.md) in two places, both named in section 3: how an unanswered approval ends a loop, and whether held time spends the runtime budget. That document's other decisions stand.
- Related: [rfc-conductor-work-ledger.md](rfc-conductor-work-ledger.md) (the `work-ledger` watch), [rfc-crew-log-wake.md](rfc-crew-log-wake.md) (how a worker's report wakes the conductor), and the goal-conductor skill's `scripts/patrol_budget.py` (agent-side renewal).
- Measured at `a97659dfc3`.

## 1. Problem

Two ways an unattended goal loop ends while its work is still live.

**An unanswered approval stops the loop for good.** A conductor patrol on a 30-minute interval with a 200-cycle cap was found stopped at cycle 47. The loop record (`97064f59` in `autonudge.json`) carried `stopped_reason: approval_stalled`. One cycle reached a tool that needed an interactive approval while nobody was at the dashboard. The prompt timed out, and the next wake turned the loop off.

On main that is by design. `notify_approval_stalled` in `src/kiro_crew/autonudge_service/timers.py` sets `approval_stalled`. `_timer` in `src/kiro_crew/autonudge_service/firing.py` then deactivates the loop with that reason and emits `expired`. Nothing turns the loop back on. A person has to notice and re-arm it, so one missed prompt costs the rest of the night's patrol.

**A work-ledger watch is ended by its own bounds.** A conductor's `watch: "work-ledger"` loop exists to wake the conductor when its workers move. Its job ends when the ledger does: every item closed is the probe's terminal settlement (`WorkLedgerProbe.observe` in `src/kiro_crew/probes/work_ledger.py`). But `_timer` checks `max_cycles` and `max_runtime_secs` first. When either runs out with items still open, the watch stops with work in flight.

The goal-conductor skill can renew its own bounds (`patrol_budget.py renew`). That depends on the model calling `monitor_update` on the right cycle. On the host that produced the evidence, that call itself needed an approval.

## 2. Goals and non-goals

Goals:

- An unanswered approval **pauses** a prompt loop. The loop stays active, fires no cycle, and spends neither its cycle cap nor its runtime budget.
- The pause ends with no re-arm by a person. It ends as soon as there is evidence that someone is back in the session.
- A work-ledger watch whose ledger still has an open item is not ended by its cycle cap or runtime budget. It ends on the ledger's terminal settlement, on a user or agent stop, or at a hard runaway backstop.
- Every one of these transitions is visible: in the goal popover, in `monitor_inspect`, and as a WARNING in the gateway log. The start of a hold also sends one notification.

Non-goals:

- Structured monitors (`gate=False` controller records). They keep their own `approval_stall` disposition.
- Changing the goal-conductor skill or `patrol_budget.py`. Agent-side renewal keeps working beside the server-side extension.
- Releasing the hold on a Slack or Discord message. On those channels, answering an approval releases it.

## 3. Design

### 3.1 The approval hold

`approval_stalled` stops being a terminal bound and becomes a **hold**.

- **Start.** `notify_approval_stalled` records the hold: the flag, plus a new `approval_stalled_at` timestamp. It writes the record under the service lock and waits for the write before it logs a WARNING, emits `updated`, and sends one notification on the monitor channel ("Monitoring loop paused — a tool approval went unanswered", naming what resumes it). That notification replaces the old "loop stopped" one, so a person away from the session still hears about it. A write that fails leaves the loop running, unheld, and sends nothing.
- **While held.** `_timer` checks the hold after the cycle cap and before the runtime budget. A held tick fires nothing and arms no timer. The reconciler skips a held loop, because no timer is the intended state. A wake that arrives anyway (a restart, a worker's push) reaches the same check and holds again.
- **Release.** `release_approval_hold` runs when a person is back. It is called from:
  - an approval answered in that session: the dashboard runner's decision closer (only when no host route decided it), the Slack and Discord approval waits, and the shared registry in `messaging/approval.py`;
  - a message a person typed into the dashboard session (`notify_user_input(human=True)`; an app's send does not count);
  - the goal popover's Play (the fire route releases the hold first, and refuses with `approval_hold_release_failed` if it cannot).

  It writes the cleared hold and waits for that write before it announces anything. Then it re-arms toward the loop's deadline. A failed write restores the hold. An agent's or app's landed turn does **not** release it: the hold waits for someone who can answer the next prompt.
- **Held time.** On release, the time spent held is added to `created_ts`, the runtime-budget clock. This amends [rfc-goal-popover-pause-play-controls.md](rfc-goal-popover-pause-play-controls.md), which rules out time-spent-paused bookkeeping for a **manual** pause. A manual pause stays as that document says. The hold is different because nobody chose it. Without the credit, a hold longer than the budget left would end the loop on its first tick after release, and the loop could not resume.
- **Visible.** The goal popover shows a held loop as paused: "Paused · an approval in this chat timed out. Press Play or send a message to resume." Play resumes it. Pause stays live, because it is the one way to stop a held loop from the popover; the stopped-goal Clear stays with stopped loops. `monitor_inspect` reports `paused_for_approval`. The REST row and the `autonudge_state` frame carry `approval_stalled`.

Rows stopped with `approval_stalled` by an older gateway keep that reason, keep their existing popover line, and stay re-armable.

### 3.2 The work-ledger bound extension

When `_timer` finds the cycle cap or the runtime budget spent, and all of these hold:

1. the loop is active and observes a work ledger (`_observes_work_ledger`),
2. its ledger holds at least one non-terminal item (`has_open_items` in `probes/work_ledger.py`; an unreadable ledger counts as no open items),
3. the loop is younger than the runaway backstop,

then the spent bound is raised instead of the loop being stopped:

| Bound | Raised to |
|---|---|
| `max_cycles` | count + a quarter of the cap, at least 10 |
| `max_runtime_secs` | loop age + a quarter of the budget, at least 1 hour, clamped to the ceiling |

Each raise is logged at WARNING and shows as the new bound on the loop record. The tick takes itself off the timer table before the update, so the update cannot cancel it before the log line runs.

**Runaway backstop.** This is the configured monitoring runtime ceiling, `monitoring.max_runtime_secs`, measured as the loop's age from `created_ts`. It is seven days as shipped, and an operator may raise it (up to 30 days). Past it, nothing is raised: the bound stops the loop as before, and the refusal is logged at WARNING. `patrol_budget.py` has its own fixed seven-day limit for agent-side renewals. The two agree at the shipped default; an operator who raises the ceiling raises only the server-side backstop.

Because a hold moves `created_ts` forward (3.1), held time does not count toward the backstop either. A loop that is often held can therefore live past seven days of wall clock. It cannot fire past seven days of unheld time, and held time fires nothing.

This amends the reading of [rfc-goal-popover-pause-play-controls.md](rfc-goal-popover-pause-play-controls.md) that a typed cycle cap is a lifetime limit, **for work-ledger watches only**. Every other loop keeps that rule.

## 4. Risks

- **A hold nobody ends.** A held loop with nobody coming back stays held. It spends no turns and no budget, so the cost is a paused row, not a runaway. It is visible in the popover and in `monitor_inspect`.
- **An orphaned item keeps a watch extending.** If a worker dies and its item never closes, the watch keeps extending until the backstop. Each step is a WARNING line, and the backstop bounds the total.
- **Held-time credit stretches the budget.** A loop held for a day runs a day later than its wall clock suggests. The credit covers only held time, and held time fires nothing.
- **A lost write.** If the store refuses a write, the hold or the release is undone in full and nothing is announced. The loop keeps the state the store has.

## 5. Security

No new authority. The hold only withholds fires, and the release only resumes a loop that was already armed. Release is driven by a person's own action in the session they own. The extension raises bounds only on a loop already armed in the conductor's own session, and only up to the operator's configured ceiling.

## 6. Alternatives considered

- **Keep the stop and notify the user.** This is what main does, plus a notification. The patrol still ends, and a person still has to re-arm it. Rejected: the failure this RFC exists for is the patrol ending.
- **Keep firing through the stall.** Each cycle would wake, hit the same prompt, be declined, and spend a cycle for nothing. Rejected: that is the waste the original stop was built to end.
- **Release the hold on a Slack or Discord thread reply.** A held channel-bound loop sends no new prompt into its thread, so today it resumes through the dashboard (Play, or a message there) or an answered approval. The hold notification says so. Wiring every channel's inbound message path is new surface, left for a follow-up.
- **Release the hold on any landed turn.** The stalled cycle's own turn lands right after its prompt times out, so the hold would end at once. Rejected.
- **Turn a long hold back into a stop after a bounded wait.** That brings back the overnight failure for a hold that costs nothing. Rejected. The one-time notification is the bound on silence instead.
- **Rely on `patrol_budget.py renew` alone.** It needs the model to act on a specific cycle, and that call may itself need an approval. Kept as the agent-side path; the server-side extension is the floor under it.
- **Exempt a typed cap from the extension.** Every conductor patrol is armed with a typed cap, so this would exempt every loop the extension is for. Rejected.
- **Extend only when the ledger moved since the last extension.** Rejected. A worker on a long build or test run legitimately writes nothing for hours, so this would end exactly the patrols that are waiting on slow, healthy work. The orphaned-item case it targets is bounded by the backstop and shows as a WARNING on every raise.
- **Make the extension an opt-in field set at arming.** Rejected for now. The only loops it applies to are `work-ledger` watches, which only a conductor arms, so an opt-in would be set on every one of them.

## 7. Open questions

None open. Two were settled in review:

1. Should an extension require that the ledger changed since the previous one? No (section 6): it would end patrols waiting on slow, healthy workers.
2. Should the hold send a notification of its own? Yes, once, when it starts (section 3.1).

## 8. Rollout

One implementation PR, [#16993](https://github.com/kirodotdev/KiroCrew/pull/16993), after this document lands. Exit criteria, each pinned by a test there:

- An unanswered approval leaves the loop active, fires no cycle, emits no `expired`, and sends one hold notification.
- A person's answer, a typed dashboard message, or Play releases the hold, and the next cycle fires with no re-arm.
- A hold or release whose write fails is undone and announces nothing.
- A work-ledger watch with an open item and a spent bound is extended and logs a WARNING. A finished ledger, a non-ledger loop, a user stop, or a loop past the backstop stops as before.
