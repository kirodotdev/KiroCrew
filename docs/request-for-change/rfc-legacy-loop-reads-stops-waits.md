---
title: Legacy monitoring loops -- reading the named pull request while held or ungated, keeping an agent's stop, and an agent-declared wait for a person
status: accepted
author: kyleseaman, with kirocrew-worker
created: 2026-10-19
last-audited: 2026-10-19
audited-at: 4fe2494b7d
doc-pr:
implementation-prs: [21377, 21621, 21622]
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Legacy monitoring loops -- PR reads while held or ungated, kept agent stops, and agent-declared waits

- Status: accepted. These are the product owner's decisions (Kyle Seaman,
  @kyleseaman), given 2026-10-19 for the stack [#21377](https://github.com/kirodotdev/KiroCrew/pull/21377)
  → [#21621](https://github.com/kirodotdev/KiroCrew/pull/21621) → [#21622](https://github.com/kirodotdev/KiroCrew/pull/21622).
  Kyle asked for each behaviour below and asked that it be recorded here rather
  than by a review override. This document amends four documents in its own text,
  so none of them is edited: [rfc-goal-loop-approval-hold.md](rfc-goal-loop-approval-hold.md)
  (`implemented`), [rfc-token-efficient-monitors.md](rfc-token-efficient-monitors.md)
  (`implemented`), [rfc-goal-popover-pause-play-controls.md](rfc-goal-popover-pause-play-controls.md)
  (`implemented`) and [rfc-work-ledger-person-wait-hold.md](rfc-work-ledger-person-wait-hold.md)
  (`implemented`). Claims about today's code were checked at `4fe2494b7d` (main).
- Author: kyleseaman, with kirocrew-worker.

## 1. Problem

A legacy loop is one armed with `monitor_start` (the `irq.py` / `autonudge_service`
stack), as opposed to a structured `monitor_watch` monitor. Babysitting a pull
request is its most common use. Four gaps on main make it waste turns or lose
its record:

- **A held loop is blind.** A loop held for an unanswered approval "fires nothing
  and arms no timer" (`rfc-goal-loop-approval-hold` § 3, *While held*). A pull
  request that merges or closes during the hold leaves the loop active and paused
  until someone comes back, and nothing can end it.
- **An ungated loop learns of a conflict late.** `gate=false` keeps a loop off the
  observation gate, so nothing reads its pull request between cycles. A head that
  goes conflicting waits out the whole interval -- often 30 minutes to 2 hours on a
  babysit loop -- before the loop sees it.
- **An agent's stop leaves nothing.** `autonudge_stop` / `monitor_stop` on an
  ordinary legacy loop REMOVES its row (`session_directive_apply_monitor.py`, the
  docstring ending "Callers that need a retained terminal record must be watching
  a structured monitor."). `monitor_inspect` then reads "not armed", the popover
  shows an empty goal form, and why the loop ended survives only as a WARNING line
  in `gateway.log`. A spent cap or budget already keeps its row; the agent's own
  stop is the common ending that does not.
- **A loop waiting on a person spends its budget.** A loop whose agent has asked
  the user a question (approve a force-push by SHA, pick between two fixes) can do
  nothing until they answer, yet every cycle fires a turn that only reports it is
  still waiting, spending the cycle cap and the runtime budget. The person-wait
  hold exists only for a `work-ledger` watch, and
  `rfc-work-ledger-person-wait-hold` § 2 lists "Holding any loop other than a
  `work-ledger` watch" as a non-goal.

## 2. Decisions

### 2.1 A held gated loop that names a pull request reads it and settles when it ends

Applies to a legacy loop armed with the gate on (`gate=true`, so it carries a
monitor) and held by an unanswered approval (`approval_stalled`) or by a declared
person-wait (§ 2.4), whose message names exactly one pull request.

- Its held tick fires no cycle and spends no bound, as before. It reads the pull
  request once (`gh_pr.fetch_state`, one `gh pr view`, no model turn) and re-arms
  `SUBJECT_READ_SECS` (300 s) out.
- A pull request that merged or closed settles the loop: it deactivates with
  `monitor_terminal`, records the outcome on its monitor (`success` for merged,
  `blocked` for closed), as the gate does, and sends the usual expiry notice. The
  held span is credited back to the runtime budget in the same write. A channel
  loop settles here too, since a held loop delivers no turn.
- The reconciler rescues such a held loop whose timer died, at the same cadence.
- A held loop that names no pull request, and a held ungated loop
  (`gate=false`) whatever it names, read nothing and keep main's behaviour: the
  held tick arms no timer and spends no cycle, the reconciler skips it, and it
  stays paused until a person comes back to the session, or stops the loop. The
  `gate=false` opt-out promises that a terminal reading never suppresses or ends
  the loop; that promise holds through a hold too, so a merged or closed pull
  request never deactivates an ungated loop or makes it refuse a resume.

This supersedes, for a held gated loop that names one pull request, these
sentences of `rfc-goal-loop-approval-hold` § 3 *While held*: "A held tick fires
nothing and arms no timer. The reconciler skips a held loop, because no timer is
the intended state." It also narrows § 4's accepted risk "A hold nobody ends":
such a loop ends on its own when its pull request merges or closes. The matching
comment at the approval-hold check in `autonudge_service/firing.py` ("the tick
fires nothing and arms nothing ... the reconciler leaves it alone") is superseded
the same way.

### 2.2 An ungated loop that names one pull request wakes early on a new conflict only

Applies to a legacy loop armed with `gate=false` whose message names exactly one
pull request and whose interval is longer than `SUBJECT_READ_SECS`.

- While it sleeps between cycles it reads the pull request every
  `SUBJECT_READ_SECS`, and ends the sleep early, once per head, when that head
  newly conflicts with its base (GitHub `mergeable: CONFLICTING`).
- That is the only thing the read may do. `BEHIND`, a red check, a merge or a
  close never wakes it early, never suppresses a cycle and never ends the loop;
  the next cycle on its own interval sees them. An unreadable pull request goes on
  sleeping.

No base document covers this; this section records it. The `gate=false`
opt-out keeps its meaning: the loop is still never screened by the gate.

### 2.3 An agent's stop keeps a legacy loop's row

- `autonudge_stop` / `monitor_stop` on an ordinary legacy loop that is running
  deactivate it in place with `stopped_reason: agent_stop` and the agent's reason
  in `stopped_detail` (credential-scrubbed, then clipped to the monitor stop
  reason bound). The popover reads it as "Paused · the agent stopped it" with the
  reason under it, and `monitor_inspect` and the stop log read the reason from the
  row.
- The row can be resumed from the popover (Play, or a new goal typed over it).
  The agent's next `monitor_start` displaces it, as it displaces a row whose cap or
  budget ran out. A person's pause (`manual`) and the Research Lab `autonudge_stop`
  tombstone are unchanged.
- A crewmate's patrol log records the stop once, on the active-to-inactive edge,
  and records the patrol started when Play revives an `agent_stop` row. No other
  resume writes a patrol entry.

This supersedes, for legacy loops, the scope line of `rfc-token-efficient-monitors`
§ *Compatibility* ("Existing AutoNudge JSON records remain valid and keep current
semantics.") as far as the agent's stop is concerned, and the out-of-scope bullet
of `rfc-goal-popover-pause-play-controls` § *Non-goals* ("what the agent's own
`autonudge_stop` call does to a loop"). It also supersedes two code pins on main
that record the removal: the `session_directive_apply_monitor.py` docstring quoted
in § 1 and the test docstring "Loops without a tombstone consumer retain the
historical remove UX." (`test/test_autonudge_stop_auth.py`), together with the
matching sentence of `docs/system-specs/modules/learn-cron-dashboard.md`
("ordinary dashboard and channel loops retain the historical remove-on-stop
behavior"). Storing the agent's words on the row reverses the pin above
`AUTONUDGE_STOP_REASON` in `autonudge_service/model.py` ("The caller's free-form
explanation is intentionally not stored on the row: it is model-authored text and
the watchdog only needs the deterministic source"): the row is now the only place
that reason survives, so it is kept there, scrubbed and clipped as above, and
rendered as text, never markup.

### 2.4 A legacy loop's own agent may declare it is waiting on a person, within bounds

- **Declare.** `monitor_update` takes `waiting_on_person: true` on a legacy
  dashboard loop. The default prompt allows it only right after the agent has
  asked the user, in that chat, a question it cannot proceed without.
  `waiting_on_person: false` releases it.
- **Held.** The loop takes the person-wait hold's fields (`waiting_on_person`, its
  start stamp) marked `person_wait_declared`. It fires no cycle. A gated loop
  reads its one named pull request each held tick and settles on merge or close;
  an ungated one reads nothing (§ 2.1).
- **Released** by a person's return, on the signs that end the approval hold (a
  message typed into that dashboard chat, an answered approval, Nudge now), with
  the held span credited back to the runtime budget, or by the agent passing
  `false`.
- **Expires.** The hold ends at the earlier of 24 hours (`PERSON_WAIT_MAX_SECS`)
  after it started and the end of the runtime budget the loop had left when it
  started. An expired wait is charged to the runtime budget, not credited, so
  repeated declarations can never keep a loop alive past its `max_runtime_secs`.
  The loop then resumes on its normal path: a spent budget stops it, otherwise its
  next cycle fires and the agent learns the wait expired.
- **Recorded.** The loop keeps why its last declared wait ended in
  `person_wait_release` (`expired`, `person` or `agent`), cleared by a new
  declaration and on every revival. `monitor_inspect` and the loop row report it,
  with `person_wait_expires_at` while a wait holds. The popover shows when a held
  wait ends and, after an expiry, that it expired with no answer.
- **Refused** where it could silence a loop no person is watching for: a
  structured monitor, a paused or stopped loop, a declaration from a turn the
  session did not produce itself (a cron injection, an app-driven turn, a
  sub-agent sharing the slot), and a loop bound to a messaging channel or a
  dashboard chat mirrored to one (the person would answer in the thread, which
  does not reach the release). A declaration while an approval hold is on records
  nothing; the two holds never overlap.
- A work-ledger read neither holds nor releases a declared wait; the ledger hold's
  own trigger and release are unchanged.

This amends the non-goal bullet "Holding any loop other than a `work-ledger`
watch." of `rfc-work-ledger-person-wait-hold` § 2: a legacy loop may also hold,
by its own agent's declaration and within the bounds above.

## 3. Non-goals

- Structured monitors (`monitor_watch`). They keep their own probes, gate and
  retained `USER_STOP` record.
- Reading anything other than one named GitHub pull request, or acting on a
  between-cycles reading other than a new conflict (§ 2.2) or a merge or close of
  a held loop's pull request (§ 2.1).
- A notification when a declared wait starts or expires. The agent's own turn is
  what tells the user it is waiting, and the turn after an expiry tells them it
  stopped waiting.
- Changing the approval hold's release signs, its credit, or the work-ledger hold.
- A lasting home for the per-loop reads. The one `gh pr view` per loop in § 2.1
  and § 2.2 moves onto the shared GitHub observation provider when that lands, or
  retires with the legacy loop path, whichever comes first.

## 4. Risks

- **One read per interval per held gated loop or sleeping ungated loop.** A
  `gh pr view` every five minutes for each such loop, with no model turn. It is
  bounded by the number of loops and costs GitHub API quota, not tokens.
- **A wake on a conflict the loop cannot fix.** An ungated loop wakes once per
  head that newly conflicts, not on every read, so a standing conflict costs one
  early turn.
- **A held gated loop that names the wrong pull request.** § 2.1 reads the one pull
  request the message names. When that is a precondition or dependency (a base PR
  the loop waits on) rather than the loop's subject, the loop settles when that PR
  merges or closes, though its own work is not done. Accepted: the settlement
  sends the usual expiry notice, so the user learns it ended. A settled loop is
  terminal, so the agent re-arms with `monitor_start` naming the subject pull
  request; `monitor_update` can only retarget the message before it settles.
- **A declared wait nobody answers.** It holds for at most a day and never past
  the loop's budget, and the time is charged. It is visible in the popover and in
  `monitor_inspect`.
- **Kept agent-stop rows accumulate.** One row per session at most, displaced by
  the next `monitor_start` and cleared from the popover like any paused row.

## 5. Implementation

- [#21377](https://github.com/kirodotdev/KiroCrew/pull/21377): § 2.1 for the
  approval hold, and § 2.2.
- [#21621](https://github.com/kirodotdev/KiroCrew/pull/21621): § 2.3.
- [#21622](https://github.com/kirodotdev/KiroCrew/pull/21622): § 2.4, and § 2.1
  for the declared wait.

[monitor-architecture](../system-specs/modules/monitor-architecture.md) owns the
shipped behaviour once they land.
