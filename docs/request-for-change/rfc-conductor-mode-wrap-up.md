---
title: "Wrap up a conductor mode chat in place: workers report and stop, the chat returns to the default agent"
status: draft
author: unstablebrainiac
created: 2026-11-21
last-audited: 2026-11-21
audited-at: 44b184c9f9
doc-pr: 40478
implementation-prs: []
tracking-issues: [39557]
supersedes: []
superseded-by: []
---

# RFC: Wrap Up a Conductor Mode Chat in Place

> **Status:** `draft`. Nothing in this document is built, and it changes no
> code. It needs a maintainer's and the owner's sign-off, because it reopens a
> recorded decision: a conductor mode chat is locked on its conductor after its
> first message. The baseline was verified at `44b184c9f9`.

## Summary

Add one action to a conductor mode chat: **Wrap up crew**. After the person
confirms, every open worker gets one bounded final turn to report what it has
and stop. The gateway closes the workers, the conductor writes one summary of
their reports into the chat, and the chat switches to the default agent. The
Board and Dashboard tabs go away, but the work ledger and the Dashboard page
stay on disk. Turning conductor mode on again in the same chat brings both back
with the same items.

The switch to the default agent commits only when the chat has **zero open
crew members**, checked under the slot lock. That is what keeps every worker
chat out of the sidebar, which is the reason in-place turn-off was set aside
on 2026-11-19.

```
person: Wrap up crew
  └─ confirm card: open items, running workers, pending approvals, worker crons
       └─ each open worker: steer "report what you have, then stop" (bounded)
            └─ worker: work_report ──▶ root's work ledger
       └─ gateway closes the line (existing conductor close path)
       └─ conductor: one summary turn from the ledger (fallback: gateway text)
       └─ switch to the default agent, admitted only with zero open members
            └─ Board and Dashboard tabs hidden; ledger and page kept
```

## Motivation

### Current state

A **conductor mode root** is a root session running a conductor template
(`crew_mode.runs_crew_mode`, `crew_mode._row_is_root`). Once its transcript has
a user message, the gateway refuses every switch off the conductor with
`409 crew_mode_locked` (`dashboard/chat_api/agent_switch.py`,
`_crew_mode_lock_refusal`). The composer offers the turn-off only before the
first message (`website/src/pages/chat/page/crewModeSwitch.ts`,
`crewModeCancellable`). The rule is recorded in
`docs/decisions/2026-10-27-crew-mode-is-a-composer-switch-to-the-conductor-template.md`
("locked after it") and
`docs/decisions/2026-11-02-an-ultra-mode-row-shows-only-what-needs-attention.md`
("It is one way").

Two changes followed in November:

- Issue [#39557](https://github.com/kirodotdev/KiroCrew/issues/39557),
  requested by maintainer iamwhatever, asks for in-place turn-off once the
  crew is idle, with the ledger kept.
- [#39568](https://github.com/kirodotdev/KiroCrew/pull/39568) built that and
  was closed on 2026-11-19: "the owner chose fork-to-a-normal-chat over
  in-place turn-off; the lock stays."
  [#39670](https://github.com/kirodotdev/KiroCrew/pull/39670) shipped the
  fork instead (`7c1f959c88`): a person's Fork of a conductor mode root lands
  on the default template (`dashboard/chat_fork_conductor.py`,
  `conductor_root_fork_template`). Its description gives the reason against
  in place: turning a chat off would pop every open worker chat into the
  sidebar, because the sidebar reads "conductor root" from the chat's agent.

### Why the sidebar objection holds for a plain turn-off

The objection is correct for a switch that runs while workers are open. The
chain on main:

1. `crew_mode.stamp_rows` finds roots with `_row_is_root`, which calls
   `runs_crew_mode` on the row's **agent**. A root moved to the default agent
   is no root. The same pass drops it from the remembered roots
   (`_known_roots.difference_update`), so it does not even count as a closed
   root.
2. A live worker's walk up its creators then never meets a root, so the row
   gets no `crew_root`.
3. The person's list hides a row only when its `crew_root` names a listed
   root: `crewModeView` and `underListedRoot` in `website/src/lib/crewMode.ts`
   on the client, `crew_mode.list_view` on the server. With no `crew_root`,
   every open worker becomes an ordinary sidebar row.

Every step reads **live** rows. A worker that is closed is not in the slot
frame at all, so it cannot pop into the sidebar. That is the property this
design builds on.

### Why Fork is not enough for this ask

Fork is the right tool for branching a conversation while the crew keeps
going. The person in #39557 wants something else: the goal is done, or they
are done with it, and the crew should stop. After a Fork:

- The original chat keeps its workers running. Their reports land on a Board
  in a chat the person has stopped reading, and the work-ledger patrol keeps
  waking that conductor.
- The conversation is split. The fork is a snapshot at fork time, so the
  workers' later reports never reach it.
- The Board and Dashboard stay on the old chat, not on the one the person
  continues in.

Wrap-up does what Fork cannot: it ends the crew's work, brings the results
into the same chat, and leaves one conversation.

## Goals

1. A person can end a conductor mode chat's crew from that chat, at any time,
   and keep talking in the same chat with the default agent.
2. Each open worker gets a chance to report its work so far before it stops.
3. The reports reach the chat as one summary turn the default agent can read.
4. No worker chat ever appears in the sidebar because of the wrap-up.
5. The work ledger and the Dashboard page are kept; turning conductor mode on
   again in the chat shows them as they were.
6. The idle case of #39557 (nothing open, nothing running) is the same action
   with nothing to wait for.

## Non-goals

- Changing Fork, or any fork of a chat that is not a conductor mode root.
- Turning a chat off without wrapping up. A switch off the conductor that does
  not come through wrap-up stays `409 crew_mode_locked`.
- Bot threads (slot `mode` `member`). They are not conductor mode roots
  (`crew_mode._row_is_mate_root`) and keep their own lifecycle.
- Incognito and Temporary chats. They cannot be conductor mode
  (`memoryModeAllowsCrewMode` in `website/src/lib/crewMode.ts`), so nothing
  changes for them.
- A Slack code channel's conductor session. It carries no `crew_mode_agent`
  stamp (`slack/code_channels/binding.py`, `resolve_session_spec`) and its
  workers are filed in the channel's folder; it is not offered the action.
- Moving, accepting or deleting ledger items. Wrap-up records reports; it does
  not decide work.
- Remote crew members (`peer_id` rows), which `liveCrewWorkers` already leaves
  out of the crew list (`website/src/lib/crewWorkers.ts`).

## Design

### 1. Entry and confirmation

The composer bolt on a conductor mode root with messages gains a menu with one
item, **Wrap up crew**. Before the first message the bolt keeps today's
turn-off, unchanged.

The item opens a confirm card filled by a read-only preview route,
`GET /api/chat/slots/{slot}/crew-wrap-up`. It answers:

| Field | Source on main |
|---|---|
| `open_items` | the root's work ledger, as the Board reads it (`dashboard/handlers/work_ledger_read.py`) |
| `workers` | live members of the root's line (`chat_api/conductor_close.py`, `_conductor_line`) with their running state |
| `pending_approvals` | members with `pending_approval` |
| `worker_crons` | cron jobs whose creating session is a member (`cron_service/model.py`, `CronJob.session_key`) |

The card says what will happen in plain words: each listed worker gets a few
minutes to report and then closes; pending approvals are rejected once; worker
crons are paused; the chat then continues on the default agent; turning
conductor mode on again later brings the Board back. **Cancel** changes
nothing.

The route uses the gate the Crew panel's routes use: owner only, app token
refused (`dashboard/handlers/crew_workers.py`).

### 2. The run

`POST /api/chat/slots/{slot}/crew-wrap-up` starts the run. It holds one
in-memory wrap-up mark on the root for its duration. While the mark is held:

- `session_create`, `session_fork` and `work_ledger_record action=bind` from
  inside the root's tree are refused with `crew_wrapping_up`, so the line
  cannot grow during the run. The close path already closes a session opened
  while the line is closing (`_close_managed`), so this refusal is a second
  wall, not the only one.
- A second `POST` answers `409 crew_wrap_up_running`.

The mark is released only after step 8 has committed or been refused. A worker
opens its own slot, so the root's slot lock alone cannot stop one from
appearing between the member check and the commit; the mark refusing every
new session in the tree until the commit is what closes that gap.

Steps, in order:

1. **Pause worker crons.** Before it pauses anything, the run writes the job
   ids it is about to pause into the root's own slot metadata
   (`crew_wrap_up_paused_crons`), which is persisted with the slot like the
   `crew_mode_agent` stamp. Then each cron in `worker_crons` is paused, not
   removed, so the person can resume one from the Schedules page. The summary
   lists them, and so does the preview of any later wrap-up, read from that
   field, so a restart in the middle of a run cannot leave paused crons that
   nothing names. A preview lists only the ids in the field that are still
   paused, so a cron the person resumed drops off. The field is cleared when a
   run's switch commits, after the summary has named them, and when conductor
   mode is turned on again.
2. **Reject pending approvals once.** Each pending tool approval of a member
   is answered `reject_once`, the Reject once decision the conductor's
   approval card already sends (`website/src/api/client/approvals.ts`,
   `decideApprovalPath`). It decides that call only, and the card named it.
3. **Final turn.** Each open member gets one steer through the path a
   conductor's follow-ups use (`session_send` with `steer: true`, see the
   crew-mode spec, "Conductor-to-worker follow-ups"). The text is fixed:
   report your current state with `work_report` (`done` if the bar is met,
   otherwise `progress` with what is finished, what is not, and where it is),
   then stop and start nothing new. A busy worker receives it in its running
   turn; an idle one starts a turn.
4. **Wait, bounded.** The run waits until each bound member has written a
   `work_report` after the steer, or its turn has ended, or the bound expires.
   The bound is one fixed value for the whole run (proposed: 5 minutes). On
   expiry the gateway cancels that member's turn through the existing turn
   cancel.
5. **Close the line.** The gateway closes every live member down the line,
   each archived to History, with the layer walk the person's close of a
   conductor uses (`_close_managed`: `session_managers.holder`, then
   `close_slot(..., hand_back=False)`). That walk follows the manager record,
   and the person's close runs after the root itself closed; here the root
   stays open. So the wrap-up run takes its set from `_conductor_line` (every
   live row stamped with this root as `crew_root`) and closes any member the
   manager walk did not reach directly, such as one handed up to another
   manager inside the tree. Closing a session already retires its monitors and loops
   (`autonudge_service/mutations.py`, `remove_by_slot`, outcome
   `session_close`).
6. **Stop the root's ledger watch.** The root's `work-ledger` loops (the
   default patrol from `conductor_patrol.ensure_patrol`, and any the conductor
   armed itself) are stopped as a system stop. `ensure_patrol` treats a
   system-stopped loop as none and replaces it, so the patrol returns by itself
   when conductor mode is turned on again and work is bound.
7. **Summary turn.** The conductor gets one turn with a fixed prompt: read the
   ledger, write one summary of each item's last report for the person, and
   record nothing new except per-item notes through `work_ledger_record`. If
   that turn fails or exceeds its own bound, the gateway appends a plain
   summary built from the ledger instead: each item's title, state and last
   report summary.
8. **Switch.** The gateway switches the root to the default template through
   the ordinary agent switch, with a new caller flag `crew_wrap_up` on
   `SlotAgentSwitchCaller`.

### 3. The commit rule that removes the sidebar objection

`_crew_mode_lock_refusal` gains one exception: a switch with `crew_wrap_up`
set, onto the default template in the template namespace, passes when the
root's line has **no live member**. The check reads `_conductor_line` under the
slot lock, in the same critical section that commits the switch, the way the
lock already reads the transcript there.

If any member is still live, the switch is refused, the chat stays in
conductor mode, and the run reports the session that would not close (the
close path already returns it in `failed_workers`). The person sees that
worker on the Board as before and can try again.

So the sidebar outcome follows from the order of operations:

| Moment | Live members | What the sidebar shows |
|---|---|---|
| During the run | some, each with `crew_root` | nothing new: the root is still a root |
| Close failed for one | that one | nothing new: the switch is refused, the root is still a root |
| After the switch | none | nothing new: there is no live worker row to lose its `crew_root` |

No change to `stamp_rows`, `list_view` or `crewModeView` is needed. The rule
those functions follow stays true; wrap-up only makes sure there is nothing
left for them to unhide.

Closed workers stay where closed sessions are: History, unnested, as today.
Their lineage in the crew log's session tree (`crew_mode.persisted_parents`)
still names the root, so when conductor mode is turned on again they are that
root's closed workers again on the Board. A person who reopens one from
History while the chat is plain gets an ordinary session that chat created, by
their own choice; that is today's behaviour for any plain chat's child.

### 4. The default agent sees the summary

The switch resets the agent session, as every committed switch does. The next
turn is a cold start, and a cold start carries the provider-agnostic history
replay from the conversation log (`dashboard/chat_turn/prompt_assembly.py`).
The summary turn is in that log, so the default agent reads it with the rest
of the conversation. No new import path is needed.

### 5. Board and Dashboard: hidden, kept, restored

- **Board.** The Board tabs (`workers`, `command-center`) are shown for a
  conductor mode root. After the switch the client drops them the way
  `turnOffCrewMode` does today (`dropPanelViews(key, BOARD_VIEWS)`). The work
  ledger files are not touched.
- **Dashboard page.** The agent switch already leaves the page alone when the
  chat leaves the conductor (`dashboard_agentic.adopt_crew_mode_board_on_switch`
  adopts only on a page nothing was adopted on). The tab is hidden with the
  Board; the page package stays.
- **Restore.** The mid-chat switch onto the conductor already works on a plain
  root with messages (`crewModeMidChat` in `crewModeSwitch.ts`). Turning it on
  makes the chat a root again, the Board reads the same ledger under the same
  session key, and the Dashboard page is the one it had. Items whose worker
  closed read as stale (`work_ledger_rules.is_stale`, `worker_closed`), so the
  conductor sees them on its first patrol and can re-dispatch.

### 6. Failure cases

| Case | Behaviour |
|---|---|
| A worker does not report by the bound | Its turn is cancelled and it is closed. The summary names it as "no final report" and quotes its last ledger report. |
| A worker is parked on a tool approval | The approval is rejected once (named on the card), then the worker gets its final turn like any other. |
| A worker is mid-push or mid-command | The steer lands in its running turn, so the command finishes first. If the bound expires first, the cancel interrupts it as a person's Stop does today; the summary says the turn was cut off and the person checks the remote. |
| A worker owns crons | Paused, not removed (step 1). Listed in the summary. |
| A worker owns monitors | Retired by the close (`remove_by_slot`, `session_close`). |
| A worker's close fails | The switch is refused, the chat stays in conductor mode, the card reports which worker. |
| A grandchild session | Closed down the line by `_close_managed`, like a person's close of the conductor. |
| The gateway restarts mid-run | The mark is in memory, so it is gone. Nothing switched, because the switch is last. Already closed workers stay closed. Crons the run paused stay paused, and the persisted `crew_wrap_up_paused_crons` field names them on the next preview and summary; the person can run wrap-up again. |
| The summary turn fails | The gateway appends the ledger-built summary and still switches. |
| A Bot thread | Not offered: not a conductor mode root. |
| Incognito or Temporary | Not offered: cannot be conductor mode. |
| Default agent is itself a conductor | Not offered: there is no plain agent to land on (the rule `conductor_root_fork_template` already applies). |
| Nothing open, nothing running | Steps 1 to 5 have nothing to do; the run goes straight to the summary and the switch. This is #39557's idle case. |

## Migration plan

Each phase is one PR and leaves main working if the later ones never land.

### Phase 1: gateway run and commit rule

The preview and run routes, the wrap-up mark, the `crew_wrapping_up` refusals,
the `crew_wrap_up` caller flag and the lock exception, the ledger-built summary
fallback, and route tests against a real `DashboardState`. No UI calls it yet.

Exit criteria:

- A wrap-up of a root with two open workers closes both, switches the root to
  the default template, and leaves `crew_mode_root` false on the root's row.
- A worker whose close is forced to fail keeps the root on the conductor, and
  the run answers that worker in `failed_workers`.
- After a successful run, `crew_mode.list_view(rows, "", agent=False)` holds no
  row that was a member before the run, because none is live.
- A switch off the conductor without `crew_wrap_up` is still
  `409 crew_mode_locked`.
- `session_create` from a member during the run answers `crew_wrapping_up`.
- The root's ledger files are byte-identical before and after the run, apart
  from the reports the workers wrote.

### Phase 2: final turns and the conductor summary

The fixed steer, the bounded wait, the cancel on expiry, cron pause, approval
reject, and the conductor's summary turn.

Exit criteria:

- A worker that calls `work_report` inside the bound has that report in the
  summary.
- A worker that does not is cancelled at the bound and named in the summary.
- When the summary turn fails, the ledger-built fallback quotes a timed-out
  worker's last ledger report, since that worker is already closed and the
  ledger is the only place its work is recorded.
- A worker's cron is paused, not removed, after the run.
- A run stopped right after the cron pause (the gateway restarted) leaves
  `crew_wrap_up_paused_crons` on the root's metadata line, and the next
  preview lists those crons.
- The first default-agent turn after the switch carries the summary in its
  history replay.

### Phase 3: composer item and confirm card

The bolt menu item, the confirm card from the preview route, the Board and
Dashboard tabs dropped after the switch, strings in every shipped locale, and
screenshots in light and dark.

Exit criteria:

- The item shows only on a conductor mode root with messages whose default
  agent is not a conductor.
- Cancel on the card sends no request to the run route.
- Turning conductor mode on again after a wrap-up shows the same items on the
  Board.

### Decision record

Phase 1 adds a decision entry under `docs/decisions/` that supersedes, in part,
the two entries named above, with a maintainer's sign-off on the record.

## Backward compatibility

Compatible. Every switch the gateway accepts today is still accepted, and the
one new switch needs a caller flag only the wrap-up route sets. No record,
route or field is removed or renamed. A chat that is never wrapped up behaves
exactly as today.

Phase 2 adds one optional slot metadata key, `crew_wrap_up_paused_crons`, as a
new row in the `FIELDS` table of `dashboard/slot_persistence/metadata_codec.py`.
The table reads only the keys it has rows for, so a gateway without that row
ignores the key. After a downgrade, the next full save rewrites the line
without it; the crons stay paused and still show as paused on the Schedules
page.

## Security considerations

- The routes are owner only and refuse an app token, as the Crew panel's
  routes do. An agent cannot start a wrap-up: there is no agent tool for it,
  and the `crew_wrap_up` flag is set only by the route.
- Rejecting a pending approval is the narrowest answer to it and is shown on
  the card before the person confirms. Nothing is approved on anyone's behalf.
- No permission or trust boundary moves. Closing sessions and pausing crons
  use the paths a person's own close and pause use.

## Alternatives considered

1. **Fork only (today).** Right for branching; leaves the crew running, splits
   the conversation, and leaves the Board on the old chat. See Motivation.
2. **In-place turn-off when idle (#39568).** Correct when nothing is open, but
   the lock still holds while one worker is parked or one item is open, and
   that is the state a person who wants to stop is often in. Wrap-up makes the
   crew idle on purpose, then does the same switch.
3. **Re-parent workers or hide them by a persisted "was a root" mark.** Keeps
   open workers out of the sidebar after the switch, but leaves sessions
   running with nobody reading their reports, and adds a second meaning to
   `crew_root` that every list must learn. Closing them is simpler and
   matches what the person asked for.
4. **Let the conductor run the wrap-up from its prompt.** No gateway work, but
   nothing would guarantee the final state: a conductor that forgets one
   worker leaves it open, and the switch would then pop it into the sidebar.
   The commit rule has to live in the gateway.

## Open questions

1. The bound: one fixed 5-minute value, or a value the person picks on the
   card. Phase 2 is blocked on this.
2. Whether a worker's cron that does not deliver to its own session (a script
   cron that only writes a dashboard record, say) should be paused too, or
   left running. Phase 2 is blocked on this.
3. Whether the summary turn should be visible as the conductor's own message,
   or rendered as a system row the person cannot reply to. Phase 2 can start
   with the conductor's message.
