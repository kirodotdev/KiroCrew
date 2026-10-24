---
title: Restart resume — pick an interrupted turn back up without a person, within a budget
status: draft
author: hickoxd
created: 2026-10-14
last-audited: 2026-10-24
audited-at: fada42f7b3
doc-pr:
implementation-prs: []
tracking-issues: [13838, 12275]
supersedes: []
superseded-by: []
---

# RFC: Restart Resume

> **Status:** `draft`. Acceptance is requested from a maintainer; the status
> flips to `accepted` when one records it in §9. Nothing is on main. Every
> claim about today's code was checked at `213339f671`. The document and its
> implementation travel in one pull request, behind a switch that is off by
> default, so the First Principles lane reads the decision beside the code it
> governs (or a maintainer records it); what a gateway restart does to a chat
> that was mid-turn is unchanged until someone turns the switch on.

## 1. Summary

A new dashboard setting, **off by default**, makes the gateway press Resume on
the user's behalf:

- after a restart, for every restored chat whose turn the previous process
  never finished (the chat that today ends in *"This turn was interrupted when
  the app restarted. Review the partial output above, then resume to
  continue."*);
- after a turn ends in an error the gateway classifies as **worth retrying**
  (a connection that stayed down past the in-turn recovery window, a session
  start that timed out once), after an exponential backoff.

Every automatic resume runs through the path the Resume button already uses
(`continue_slot_turn`), so it refuses the same states the button refuses, lands
the same kind of row, and is audited the same way. A **per-chat budget** — at
most N automatic resumes in a rolling window of M minutes, defaults 5 in 60 —
bounds what the automation can spend. When the budget is spent the chat gets
one notice and a notification, and the Resume button is back in the person's
hands; a human action (Resume, a typed message) resets the budget.

Permissions are **not** changed. A chat comes back after a restart in the
interactive mode, as it does today, because an ad-hoc per-chat Trust is held in
memory by construction and the review rule forbids persisting it. What this
design adds is the sentence that is missing today: the restore records which
mode the person last saw the chat hold and, with the switch on, when the chat
comes back holding a different one, lands one status row saying so and naming
the remedy (§4.6).
The record is never read back into the mode. A grant that was live when the
gateway *crashed* is [rfc-crash-only-session-trust.md](rfc-crash-only-session-trust.md)'s
to restore; this design composes with it and adds no second record.

## 2. Motivation

At `213339f671`:

- A turn that was admitted and never torn down leaves a marker on the
  session's metadata line (`turn_in_flight_generation`, plus a copy of the
  opening row). On the next boot, `_reconcile_local_turn_marker` in
  `src/kiro_crew/dashboard/slot_persistence/turn_marker.py` turns the marker
  into one `error` row tagged `meta.kind = "gateway_restart_interruption"` with
  the wording quoted above (unless the tail already proves the interruption —
  an unanswered user row, a cut recovery or nudge turn — in which case no row
  is added), and clears the marker. That is the whole recovery: the composer
  offers **Resume** (`ChatInput.tsx`, gated on
  `selectTurnInterrupted`, the mirror of `is_turn_interrupted` in
  `src/kiro_crew/dashboard/state_parts/transcript_rows.py`) and waits.
- Nothing presses it. `restore_open_slots_async` and
  `restore_recent_sessions_async` (`src/kiro_crew/dashboard/chat_persistence.py`,
  driven from `_restore_dashboard_sessions` in
  `src/kiro_crew/dashboard/server_runtime/session_restore.py`) rebuild the
  slots and return. `dashboard.restore_sessions` decides which chats come back,
  not what happens to a turn inside them.
- The gateway already retries a great deal **inside** a turn: a transient
  provider error is retried `TRANSIENT_RETRIES` times on
  `transient_retry_delay`'s exponential curve, a dropped connection is
  re-prompted for `CONNECTION_RECOVERY_WINDOW_SECS` (300 s) with the delay
  capped at `CONNECTION_RETRY_MAX_DELAY` (`src/kiro_crew/llm_helpers.py`), a
  stalled or wedged turn is recovered up to `STOP_RECOVERY_MAX_RETRIES` times
  (`src/kiro_crew/dashboard/chat_runner.py`). Each of those retries lands a
  `RecoveryCard` row with an `automatic recovery` prefix. When the ladder is
  exhausted the turn **ends** — with `TRANSIENT_GIVE_UP_TEXT`
  (*"Connection unstable — please try again."*, `meta.notice =
  "transient_give_up"`) or a `session_start_failed` row — and from that point
  on, the only continuation is a person.
- On a session this turn natively resumed (`session/load`), kiro-cli's log
  lacks the turn the process died on (it logs a prompt only once it is
  answered), so `_run_chat` re-injects that turn from the slot window
  (`build_interrupted_turn_preamble`) for the Resume press and the in-turn
  recoveries. The set of bodies that get this is a literal in the runner today;
  any new continuation body must join it or the model is told to finish a
  request it cannot see.
- The continuation itself exists and is shared: `continue_slot_turn` in
  `src/kiro_crew/dashboard/chat_api/continue_turn.py` (imported as
  `chat_handlers.continue_slot_turn`, the one import path the `chat_api/`
  owners keep) is what the button
  (`api_chat_slot_continue`) and the `session_retry` / `session_continue`
  tools (`retry_target`, `continue_target` in
  `src/kiro_crew/dashboard/session_control.py`) all call. It takes the slot
  lock, refuses a running slot, a stop in progress, a pending queue, a pending
  approval, running sub-agents, an empty transcript and a session start that
  already failed twice in a row (`_SESSION_START_REPEAT_REFUSAL_AT`), picks
  `_MANUAL_RESUME_MSG` or `_MANUAL_CONTINUE_MSG`, queues it at the head as a
  `SYNTHETIC_RECOVERY_KIND` entry, writes the `dashboard_continue` SEL line
  and dispatches. Nothing in it is specific to a mouse click.
- Trust is in memory. A slot is constructed with `_trust = False`,
  `_trust_reads = False`; the metadata codec
  (`src/kiro_crew/dashboard/slot_persistence/metadata_codec.py`, `LINE_ORDER`)
  persists no approval field; `_notify_restart_dropped_grant`
  (`src/kiro_crew/dashboard/server_runtime/safety_grants.py`) tells the
  operator a global grant was lost and restores nothing. A crewmate's declared
  `agents.<name>.approval_mode` is applied by `_seed_member_approval`
  (`src/kiro_crew/dashboard/handlers/members_thread.py`, re-exported by
  `handlers/members.py`) from the open-thread route and, since
  [#23010](https://github.com/kirodotdev/KiroCrew/pull/23010), by
  `crewmate_approval.seed_for_turn` as each turn of the thread is admitted, so a
  DM thread the boot restore rebuilt takes its profile's mode at its next turn.
  Nothing records which mode a chat held, so nothing can say it changed.

What this costs: a person who left work running comes back to a chat that
stopped the moment the app restarted — an update, a watchdog exit, a host
reboot — and stayed stopped. The session, its context and its on-disk progress
were all intact; one injected message would have carried on. Reports with this
shape: [#13838](https://github.com/kirodotdev/KiroCrew/issues/13838) (a goal
conductor stalled by a restart), [#12275](https://github.com/kirodotdev/KiroCrew/issues/12275)
(armed loops stalling after a restart dropped trust),
[#13853](https://github.com/kirodotdev/KiroCrew/issues/13853) (a turn that
dies leaves only the inline Resume error),
[#4756](https://github.com/kirodotdev/KiroCrew/issues/4756) (the turn ceiling
strands unattended work).

## 3. Goals and non-goals

### Goals

1. A chat the restart cut off carries on by itself, once the gateway is up.
2. A chat whose turn died of a fault that a retry can plausibly cure gets that
   retry by itself, after a backoff.
3. The automation can never spend more than a stated budget per chat, and the
   budget survives a restart so a crash loop cannot replay it.
4. Every automatic resume is recognisable as such — in the transcript, in the
   SEL log, and to the model that reads it.
5. Nothing here widens what an agent, an app token or a cron can do; the
   automation runs with exactly the authority the chat already had.

### Non-goals

- **Persisting or restoring any approval mode.** The `backend-security-controls`
  review rule says *"Never persist an ad-hoc grant across a restart"*;
  [#6381](https://github.com/kirodotdev/KiroCrew/pull/6381) was blocked under it;
  [rfc-crash-only-session-trust.md](rfc-crash-only-session-trust.md) asks for
  the one amendment that has a safety argument (a crash is not a re-consent
  point). This document asks for none: the mode a chat held is *recorded* so the
  person can be told it changed (§4.6), and nothing reads that record back into
  the flags. `test_trust_flags_not_restored` stays exactly as it is.
- **Auto-continuing a turn the ceiling stopped** (`turn_timeout`). That is
  [#4756](https://github.com/kirodotdev/KiroCrew/issues/4756), parked by the
  maintainers as a policy question about a safety backstop; this design does
  not pre-empt it. A `turn_timeout` row is on the never-retry list (§4.3).
- **Retrying faults a retry cannot cure**: a sign-out (`auth_required`), an
  unentitled model (`model_unentitled`), a spent allowance (`usage_limit`), a
  context overflow (`context_overflow`), a refused app agent
  (`app_agent_refused`), a content-filter refusal (the fallback-model path owns
  that), an approval nobody answered.
- **Resuming a turn the user ended.** A trailing Stop card, a `/compact`
  answered by its notice, a give-up row the ladder marked `stopped` (a Stop
  from a linked channel writes no card here), and a chat the user closed
  (`chat_close_interruption`) are deliberate endings, exactly as
  `is_turn_interrupted` and `continue_slot_turn` already treat them.
- Relay (remote crew) slots, archives, incognito and temporary sessions: their
  turns are not this gateway's to resume, or nothing of them persists.
- Any change to the in-turn retry ladders. They stay as they are; this design
  starts where they give up. One row of theirs is touched after the fact, and
  only by this design's own act: a give-up row the error pass has resumed is
  stated as settled (§4.8). A row a person resumed, or nobody did, reads
  exactly as it does today — with the switch off, no transcript changes.

## 4. Design

### 4.1 Settings

Three keys on `DashboardConfig` (`src/kiro_crew/config/sections_dashboard.py`), each with
`_meta` label and help, read through the section builder like their
neighbours, hot-applied (no restart; the boot pass reads the config the boot
already loads, the error-time pass reads the live snapshot):

| Key | Type / default | Meaning |
|---|---|---|
| `dashboard.resume_interrupted_turns` | bool, **false** | The switch over both passes and the notice in §4.6. Off: no automatic resume is armed or dispatched and nothing is told at restart. (The record of what the person last saw is kept either way, so the first boot after the switch is turned on compares honestly.) |
| `dashboard.resume_retry_limit` | int, **5** (1–20) | Automatic resumes allowed per chat inside the window, restart and error resumes counted together. |
| `dashboard.resume_retry_window_minutes` | int, **60** (5–1440) | The rolling window the limit is counted over. |

Settings → Chat gets a `SettingsToggle` for the switch directly under
*Restore Sessions*, and — only while it is on — a stepper for the limit and a
select for the window, the same nesting *Restore Sessions* uses for *Restore
Window*. The help text of the switch says what it does — after a restart, or
after an error a retry can fix, it presses Resume for you, up to the per-chat
limit below — and stops there; what that spends, and what it does **not** do (a
Trust picked in the composer still ends at a restart, and the chat says so when
that happens, §4.6 — the sentence the Trust picker is missing today, see
[#12275](https://github.com/kirodotdev/KiroCrew/issues/12275)), is the warning
list's, below, so the default is off.

The switch does not require `restore_sessions`: foldered and pinned chats are
restored regardless, and a pinned chat is the likeliest one to hold a day's
work.

The warning list under the switch holds risks only — the repeated work, the
model turns and tool calls spent with nobody watching, and the Trust kept
after an error — one bullet each, the irreversible one (a half-applied edit, a
message sent twice) apart from the routine spend (UX review: one sentence
holding both let the first hide behind the second). The restart half of the
approval rule (a resumed turn that needs a tool approval waits for the person)
is the Trust bullet's second sentence — the boundary of that risk — rather than
a bullet of its own or a sentence in the grey help: a reassurance bullet
dilutes the risks beside it (UX review: readers learn to skim amber text), and a
grey sentence stating only the restart half read, skimmed, as every automatic
resume stopping at an approval (UX review, a later round). So the rule is read
in one place, risk first, in one style of text.
The Trust bullet names the control the way the composer labels it — *the
chat's Trust approval mode*, the Trust segment of the Approval mode picker —
not "Trust mode", a name no control carries (UX review: a newcomer would look
for one).

The window row is labelled *Resume limit window*, not *Limit resets after*
(UX review): the window slides — each stamp ages out on its own, one window
after it was spent — so the count never resets at a point in time, and a
label promising a reset would describe a timer the budget does not have. Nor
*Count resumes over* (UX review, a later round): the row carried *Count
resumes over the last* until the same lane read it as a fragment where the
command palette lists labels on their own, and two words shorter it is the
same fragment. The row's description states the rule the two values make
together, with the live values, and opens with the rule — *At most 5
automatic resumes per chat in any 1 hour.* — not with a pointer back to the
stepper (UX review: a reader landing on the row from the command palette met
"Together with the limit above" before anything the row decides). The window
is spelled out in that sentence and in the notices (`1 hour`, `5 minutes`,
localized by Intl on the web side) while the select keeps its `1h` (UX
review: inside prose the abbreviation reads like a typo). The sentence's second
half says what the limit decides — *At the limit the chat pauses and asks you
to press Resume, which resets the count.* — in the open, moved there from the
stepper's ⓘ tip (UX review, a later round: a person choosing the number needs
that before choosing, and a tip is read after); the tip keeps only the detail
the sentence does not carry, that restart and error resumes are counted
together. The limit row keeps its label *Max automatic resumes per chat*: the
suggested *Maximum* was argued from "the other labels use whole words", and
the Settings page already has *Max Turns per Subagent* and *Max Concurrent
Subagents*.

### 4.2 Which turns qualify

A chat is a **restart candidate** when three things hold after its window is
loaded and the reconcile in `turn_marker.py` has run. The metadata line
carried a turn-in-flight marker — the reconcile's return value, kept on the
slot for this process only as `_restart_cut_turn`: the previous process
admitted a turn it never finished, which the transcript alone cannot show, and
which an interruption row left by an *earlier* boot does not show either, so
such a chat is not resumed on a later boot. The tail reads as interrupted. And
the newest turn ended in no terminal error row at all (the marker's row is only
appended when the tail did not already prove the interruption; the in-turn retry
ladder's *pending* row, `meta.kind = transient_retry`, marks a recovery that was
queued, not how the turn ended, and is looked through — a restart during the
ladder's wait leaves it as the newest error) or in the restart row itself — a
chat whose newest turn ended in some other error, say an older
`session_start_failed`, was already failing for another reason and is not
resumed on the restart's account.

Neither pass touches a chat an app owns (`slot._app`): its app re-grants its
own trust and resumes on its own terms, and a gateway-authored continuation
would run behind its back. Nor a relay archive, a closing chat, or a chat in a
restricted memory mode (incognito, temporary), whose transcript the person chose
to keep private.

A chat is an **error-retry candidate** when a local turn ends by appending an
`error` row whose structural tag is on the retryable list:

| Tail | Retry? | Why |
|---|---|---|
| `meta.kind = gateway_restart_interruption` | yes (boot pass) | the turn was cut by the process, not by anything in the turn |
| `meta.notice = transient_give_up` | yes, with backoff | the in-turn ladder gave up on provider weather; minutes later is a different sample |
| `meta.kind = session_start_failed` | yes, once | the first Resume is the retry the kind's own comment says it deserves; `_SESSION_START_REPEAT_REFUSAL_AT` refuses the second identical start and this design does not override it |
| `meta.kind = retry_invited` | yes, with backoff | the runner's own "please retry" rows (connection lost, session busy, turn or tool stalled, a shared agent process that kept restarting): their writers stamp the kind when the in-turn recovery has given up and a retry is the advice; bounded by the budget like every other |
| `turn_timeout`, `auth_required`, `model_unentitled`, `usage_limit`, `context_overflow`, `app_agent_refused`, `chat_close_interruption` | **no** | §3 non-goals: a retry cannot cure it, or the user ended it, or another decision owns it |
| an `error` row with **no** structural kind, or a kind off this list | **no** | an approval prompt nobody answered or that had no time left, "Session stuck", an `AcpError` nobody classified, a writer added later without a tag: nothing is read off the row's words, so a terminal error is retried unattended only once its writer says so |
| a turn that failed before it started (`meta.turn_failed`) | **no** | the send never went out; the person's own retry is the fix |
| a Stop card anywhere in the newest turn, a `/compact` answered by its notice, a give-up row marked `meta.stopped` | **no** | the person's recorded intent that nothing continues on its own. The ladder's give-up writers mark the row `stopped` when the Stop signal is what ended the backoff (`transient_give_up_meta`, `chat_utils.py`): a Stop pressed on the dashboard also writes a Stop card, but a Stop issued on a linked channel (Slack's Stop, a transport's stop verb) moves the session's stop counter alone and writes no card on this slot, so without the mark the pass would resume a turn the person had just stopped (Opus review, rev 46) |

The retryable list is a frozenset of structural kinds in `restart_resume.py`
(`RETRYABLE_ERROR_KINDS`; the ladder's give-up row carries `meta.notice`
rather than `meta.kind` and is handled beside it). It has no "everything else"
entry and matches no wording, so a new terminal error is **not** retried until
its writer tags it — fail closed, the direction every other authorization list
in the gateway takes. The tag is `retry_invited`
(`chat_utils_injected_rows.py`), stamped by the ladder-end writers in
`chat_turn/turn_reply_seams.py`, `chat_turn/acp_recovery.py` and
`chat_turn/recovery.py` whose words ask for a retry; a terminal row that asks
for something else — a new chat, an approval — stays untagged and is never
retried on its own.

Every candidate is then subject to `continue_slot_turn`'s own refusals,
unchanged: running, stopping, queue pending, approval pending, sub-agents
running, empty, `session_start_repeat`. A refusal is logged at `info` and the
stamp spent for it is taken back in memory — nothing was dispatched; the line
keeps it until the next save (§4.4), so a gateway that dies in that gap counts
one resume that never ran, which is the fail-closed direction.

### 4.3 Mechanism

One module, `src/kiro_crew/dashboard/restart_resume.py`, with two entry
points and no state of its own beyond what the slot carries — plus two small
modules beside the codec's facade, since the codec is already 1,700 lines and
each is a job of its own: the validator of the persisted budget list
(`dashboard/auto_resume_stamps.py`, re-exported by the dispatcher) because the
restore reads that list for every slot, switch on or off; and the readers of
the live approval mode (`dashboard/approval_mode_record.py`: the mode in the
picker's words, the before-picture of a pick, the dirtying that makes the
record follow a pick), shared by the codec's line writer, the picker route and
the dispatcher, and re-exported by the codec.

**Off the boot path.** The dispatcher is optional and off by default, so
nothing that runs before the dashboard socket binds imports it (AUTOSDE
`no-new-work-on-gateway-boot-path`: gate the import, not just the handler).
Neither facade (`server`, `chat_handlers`) imports it at module level; the
four request-time callers (a send, a Resume, a Stop, a close) and the
runner's turn end import it on use, after the socket is bound (a cached
lookup after the first; the module itself defers the config loader to the
armed task); the restore's per-slot metadata read uses the codec-side
validator; and the restore imports the dispatcher only when the switch is
on, right where it schedules the boot pass — both passes return at once
without the switch, so the gate changes nothing they did. A subprocess test
imports both facades and applies a tampered metadata line through the codec
and asserts the module was never loaded.

**The boot pass.** `_restore_dashboard_sessions` gains one call after both
restores and the counter reseed — only with the switch on —
`restart_resume.start_boot_pass(state, cfg)`,
which runs ONE background task — the permissions notice pass (§4.6) first,
then `resume_after_restart` — so the server's listen, and `KIROCREW_READY`
behind it, wait on neither: each pass loads transcripts the restore deferred,
one per chat concerned, which is work that grows with the person's session
count and has no place on the boot path. The order inside the task is what
keeps the notice ahead of any automatic dispatch. The pass looks only at slots
whose metadata line carried the
turn-in-flight marker (the restore's first reading of it), loads each such
chat's deferred window off the loop, selects restart candidates (§4.2), and
for each one calls `continue_slot_turn(state, slot,
directive_user_origin=False, via="restart_resume", body=..., extra_meta=...)`
with a short stagger between chats (2 s; the first after the cautious-boot
pause and a 5 s settle so the slot broadcasts have landed) so a dashboard with
thirty interrupted chats does not start thirty agent processes in one instant
on a host that may have just crashed. One chat's failure is logged and
skipped rather than ending the pass. The switch is read again from the live
snapshot before each dispatch, so turning it off during the pause, the settle
or the stagger ends the pass. A chat the user touches before its turn comes up
(a typed message, a press) is skipped: their action clears the marker flag the
candidate test re-reads — a send or Resume through `reset_budget`, a Stop or a
close through `cancel_armed_retry`, which clears the flag without touching the
spent budget; so a Stop on the idle restored chat, which writes no Stop card
(the route's no-op branch), still ends the resume — a Stop card in the
transcript ends the turn on purpose as well, and an action that lands after
the dispatch began is caught under the lock by
the epoch it moved; a chat whose turn finished meanwhile is refused, since every
automatic dispatch passes `require_interrupted=True`.

**The error pass.** The runner's once-per-turn end (`_finish_queue_cycle` in
`chat_turn/queue_cycle.py`, once the slot is idle) gains one call,
`restart_resume.schedule_after_error(state, slot)`. It declines while the
switch is off in the config snapshot the process holds — read before anything
else, since with the switch off this is the whole cost of the call at the end
of every turn — then reads the tail through the same classifier the boot pass
uses, and otherwise arms a per-slot `asyncio` task that consults the budget
(§4.4), sleeps the backoff, reads the switch again (turning it off during the
wait stops the retry), and then calls
`continue_slot_turn(..., via="error_resume")`. The task handle is stored on
the slot (`_auto_resume_task`) and cancelled by anything that makes it moot: a
person's send, a person's Resume, a Stop, a close. Each of those also moves a
per-slot epoch that the dispatch re-reads under the slot lock right before its
queue insert, so a cancel that lands after the backoff but before the insert
refuses the dispatch rather than leaving a gateway-authored entry at the head
of the queue. The same hook reads the switch once more: turning it off moves
no epoch, and the budget write the dispatch awaits (§4.4) is a window in
which it can have been turned off since the pass last read it, so a switch
that reads off there refuses the insert too and the stamp is taken back.
A Stop issued on a linked channel (Slack's `!stop`, a transport's stop verb)
against the idle chat reaches none of those: it moves the session manager's
Stop count for the session the chat's turns run on and nothing on the slot
-- no Stop card, no epoch, no cancel. So the pass records that count when
it arms the retry, drops the retry when the count has moved by the end of
the backoff, and the hook compares it a last time under the lock; the boot
pass records it per candidate -- before the chat's deferred window load,
which awaits a worker thread -- and skips a chat whose count moved during
that load, the pause, the settle or the stagger (GPT review, revs 47 and
48). The same load is why the codec's second reading of the restart marker
(`AppliedMeta.settle`, once the window is in) combines with the flag as it
stands instead of assigning over it: a Stop that lands during the load
clears the flag with nothing else on an idle chat to show for it. The
manager records a Stop only for a key with a live session or an open replay
gap, and a chat with a resume pending has neither -- a restored one before
its first turn, one whose failed turn tore its session down -- so each pass
arms the key first (`SessionManager.arm_resume`, counted per chat, keyed
canonically like the gap) and disarms it when the resume dispatches or
stands down; a Slack `!stop` on that idle thread is then recorded and the
compare sees it (GPT review, rev 49). For the boot pass the arming is
synchronous and as early as the chat exists: the restore arms the cut
chats right after the open tabs are rebuilt (`arm_cut_chats`, once per
chat, the reading kept on the slot as `_restart_stop_count`), before its
own later awaits and before the boot task -- whose notice pass awaits
transcript loads ahead of the resume pass -- exists; the pass compares
against that reading and every armed key is disarmed when the task ends,
however it ends (GPT review, rev 50). There is never more than one armed
per slot.

**Chats another automation drives.** Neither pass needs to know who else
drives a chat, because nothing else re-sends a turn that ended in an error,
and nothing can run alongside a resume. Every continuation of a dashboard chat
— the Resume button, an app's Continue, an agent's `session_retry` /
`session_continue` (a conductor re-driving its worker), and the two passes —
goes through `continue_slot_turn`, which refuses a running or queued chat and,
asked for an interrupted turn, a turn that is no longer one; a chat runs one
turn at a time; and the error pass re-reads the tail right before it dispatches
and takes its stamp back on a refusal (§4.4). So when two drivers aim at the
same cut-off turn, the second to arrive is refused, whichever it is: the work
runs once. A cron delivery or a monitor loop does not re-send the failed turn
at all — it queues its *next* turn on its own schedule, which the queue
serialises behind a resume. Sub-agent runs are not dashboard turns: they run in
`subagent_manager`, never reach `_finish_queue_cycle`, and are not restored
slots, so neither pass can touch a conductor-dispatched worker. The one driver
that does re-grant and re-continue on its own terms is an app, and an
app-owned chat (`slot._app`) is excluded from both passes (§4.2).

**What the model reads.** Two bodies with their own prefix lines, `[Continue
— automatic after a restart]` and `[Continue — automatic after an error]`,
registered beside `MANUAL_RESUME_RECOVERY_PREFIX` in
`state_parts/injected_turns.py` and in the
`RecoveryCard` prefix table. The shared text tells the model that the gateway
resumed the turn and that no person has looked at the partial output above —
so a model that was about to ask the user something does not read a press that
never happened as an answer — and that it must check the current state before
repeating a step. `continue_slot_turn` takes the body as an optional argument;
the button and the tools are unchanged. Both bodies join the runner's
interrupted-turn set (`_INTERRUPTED_TURN_RESUME_MSGS` in
`chat_utils_turn_recovery`, which replaces the literal of §2), so on a natively
resumed session the lost turn is re-injected for an automatic resume exactly as
for a press; the runner leaves
the gateway's own assistant-role system notices (§4.6's among them) out of the
rows it hands the builder, since they sit in the span it quotes as the model's
partial answer and are not the model's words.

**What the resume may repeat.** The manual row asks the person to *review the
partial output above, then resume*; an automatic resume skips that review by
definition. The model is told to check the current state before repeating a
step, but it reads only the transcript: a step whose effect lives outside it —
an edit half applied, a message already sent, a migration part-way through —
can be run twice. That is the second cost of the switch, after the model turns
it spends, and the setting's help text, its visible description and the config
help say so; the switch stays off by default for both reasons. A pass that
skipped chats whose newest turn ended inside a tool call was considered and not
taken: the cut most often lands mid-tool, so the skip would exclude most of what
the feature is for, and the manual Resume carries the same repeat risk today.

**What the person sees.** The `RecoveryCard` folds the row under a new title,
*"Resumed automatically"*, with a detail line that carries the cause and the
count: *"after the app restarted · attempt 1 of 5"* or *"after an error ended
the turn · attempt 2 of 5"*. The count comes from the row's `meta`
(`autoResume`, written by the dispatcher at queue-insert time through
`extra_meta`), never recomputed by the client, and is dropped when the stamp is
absent or malformed.

### 4.4 Backoff and the budget

**Backoff** applies to error retries only; a restart resume is dispatched on
the boot pass's stagger. An error retry that would be the n-th automatic
resume of the chat within the window (restart and error resumes counted
together) waits `min(30 s × 2^(n−1), 10 min)` plus up to 25 % jitter — the same curve
shape as `transient_retry_delay`, with a base sized for "the in-turn ladder
already spent ~5 minutes and gave up" rather than for a first 5xx. Concretely:
30 s, 60 s, 2 min, 4 min, 8 min.

**The budget** is `resume_retry_limit` automatic resumes per chat in the last
`resume_retry_window_minutes`, counted over restart and error resumes
together. It is enforced at dispatch time from a per-slot list of wall-clock
stamps, `_auto_resume_stamps`, that is:

- **persisted** on the metadata line as a new slot-owned key,
  `auto_resume_stamps` (a bounded list of floats; `LINE_ORDER`, `MERGE_ORDER`
  and `history.SLOT_OWNED_META_KEYS` as the codec's docstring prescribes), so a
  gateway that restarts, resumes, crashes on the resumed turn and restarts
  again counts the earlier resumes. Without this a crash loop would replay the
  resume on every boot; so the stamp is **committed before the resume is
  queued**: the dispatcher appends it and awaits a fenced metadata merge of the
  list (`_commit_budget`, the same merge-and-guard the turn marker uses), and
  only a write that completed lets the dispatch go on to `continue_slot_turn`.
  No log, a closing chat, a line that does not exist, a guard that refused, a
  failed write or a write the log skipped (a legacy row-first transcript keeps
  its first line, and the merge reports that no-op as success unless the caller
  requires a write, so the commit does) refuses the resume and takes the stamp
  back in memory: no automatic provider work ever runs on a budget that exists
  only in memory. The
  turn marker's own awaited save then carries the same list again, and carries
  the **empty** list after a person's reset (`_auto_resume_reset_pending`) so a
  gateway that dies before the 5 s flush does not read the spent budget back.
  A reset can land while a stamp's write is in flight; the write is fenced to
  the list it carries, decided under the line's lock, so a reset that landed
  first refuses it (the person acted: the resume is refused anyway), and a reset
  that landed after it leaves the flag pending, so the person's turn marker
  carries the empty list over the spent stamps the write put on the line.
  The marker clears the flag only on a **confirmed** write: its merge answered
  true, or -- when the line did not exist yet -- its forced-save fallback did,
  judged by its result and not best-effort (a best-effort save answers true to
  a write that failed). A fallback that failed or was refused leaves the flag
  pending, re-arms the flush and still admits the turn, so the next marker
  carries the empty list again.
  The read is validated like the turn marker's: not a list, more than 64
  entries, a non-finite or future stamp, an integer too large for a float (the
  read never raises: it runs inside the slot restore, where an exception would
  make a tampered line a chat that cannot be opened) → the list is treated as
  **full** (64 stamps dated now; fail closed for the automation for one window
  from the read, which a person's send or Resume clears sooner; the manual
  Resume is untouched), a warning names the slot and the shape it refused, and
  the pause notice states only the pause (*is paused here*) and leads to the
  remedy — the cause is the operator's, in the log, not something the person can
  act on, and no duration is named: the sentinel stamps age out after one
  window, but that only re-allows automatic resumes for later interruptions,
  nothing re-arms this chat's cut-off turn (UX review) — rather than quoting the
  sentinel count — the sentinel is told apart by its size (the gateway never appends past
  the limit, whose ceiling of 20 is far below 64), so a limit the person lowered
  below the stamps already in the window is told apart too, and that row states
  the new limit — *the limit is now 2 resumes in 1 hour* — rather than the count
  over it, which blamed an overrun the person never chose (UX review);
- **reset** by a person's own action on the chat — a typed message, a Resume
  press — on the same principle `session_start_failure_streak` uses: new
  information from a person starts the count over. Neither an automatic resume
  nor an agent's `session_retry` / `session_continue` (which carry no human
  origin) resets it: the budget bounds exactly what no person asked for. Who
  counts as a person FOR THE BUDGET is one rule on both routes a person can
  press — `api_chat` and the Continue endpoint reset only with no app token
  AND no attested cron creator — and it is a separate question from the queue
  entry's provenance (`directive_user_origin`, the send route's
  `not request_app`, which directive handling and the drain read and which
  both routes still pass unchanged): `continue_slot_turn` takes the budget
  decision as its own `resets_budget` argument, so a cron's Continue
  dispatches with exactly the provenance a cron's send has and, like it,
  leaves the budget alone (Opus review: the Continue endpoint had derived the
  reset from the app token alone, so an attested internal caller with no app
  reset the budget that exists to cap unattended resumes; First Principles
  review: the first fix narrowed the provenance flag instead, which a cron's
  send does not do — `test_a_crons_continue_leaves_the_budget_alone_like_its_
  send` pins both halves, red on the old route for each).

On exhaustion the chat gets one assistant-role system notice
(`meta.kind = "auto_resume_paused"`, drawn by the existing `NoticeCard`),
*"⚠️ Automatic resume is paused here: its limit of 5 resumes in 1 hour is
spent. Pressing Resume also resets this chat's automatic-resume count."* — it
sits under the error row it answers, whose Resume button and own "try again"
are already there, so it adds only what that press does besides (UX review: a
second "press Resume" made the two rows read as two problems with one shared
button), and it names the count, since this row is all a reader arriving from
the notification sees of the budget (UX review) —
one `state.notify("agent", ...)` so the notification centre and a linked
channel hear it — its body opens with what happened, *Example session:
automatic resume used all 5 resumes in 1 hour. Press Resume in the chat to
continue.*, because the feed row glues the chat's title to it and cuts it off
after a few words, where the row's *its* would have no referent (UX review) —
and the Resume button, which was never removed. The notice is
idempotent per budget: nothing is added while a pause notice already stands
with no automatic resume after it in the transcript, so a crash loop that
reaches exhaustion on every boot, or an unattended turn that fails again while
the budget is spent, does not stack rows; a new notice is warranted only once
the automation has resumed again and spent the budget again. The transcript
then reads exactly as it does today.

**Accepted gap: a restart during the backoff drops the armed retry.** The
error pass's retry is an `asyncio` task in the running process and nothing of
it is written down. A gateway that restarts (an update, a crash) while that
task is inside its wait — up to about ten minutes after the error — comes back
to a chat whose newest turn ended on the error row. That chat carries no
turn-in-flight marker, so the boot pass (§4.3) does not look at it, and
nothing re-arms the retry. What the person finds is the row the error left,
"please retry", which is exactly the state the chat is in without the switch;
no budget stamp was spent, because the stamp is written at dispatch, not when
the retry is armed. The gap is accepted rather than closed because closing it
needs a persisted pending-retry record on the metadata line (written when the
retry is armed, cleared when it runs or is cancelled, read fail-closed at
restore, re-armed by a third boot-pass step that must load the chat's window)
whose only consumer would be this coincidence of an error, a restart and a
ten-minute window — and whose failure modes (a stale record re-arming a retry
the person had moved past) are worse than the gap. It is pinned by
`test_a_restart_during_the_backoff_loses_the_armed_retry`; a report of it
biting in practice reopens the question.

### 4.5 Boot-pressure and crash-loop guards

- The boot pass respects `cautious_boot`: it awaits
  `cautious_boot.pause_before("restart resume")` before its own 5 s settle, so
  when the previous instance wedged minutes ago the pass waits with the same
  delay class the restore groups do.
- The stagger (2 s per chat) is a floor, not a cap; the session pool's own
  limits still apply to each dispatched turn.
- A **gateway-level** crash-loop guard is deliberately *not* added: the
  per-chat budget, persisted, already bounds the total to `limit × chats` per
  window, and a chat whose resumed turn is what crashes the gateway exhausts
  its own budget in five boots. A storm detector would need a record of boots
  the gateway does not keep today; §9 Q4 asks whether maintainers want one.

### 4.6 Permissions: tell, do not restore

The resumed turn runs under whatever approval mode the slot holds when it is
dispatched — which, after a restart, is interactive, because trust is in
memory by construction. For a person at the keyboard this is the status quo:
the first tool call shows an approval card, and the card still offers "trust
this session". For an unattended chat it is the stall
[#12275](https://github.com/kirodotdev/KiroCrew/issues/12275) describes, and
today nothing says so. Stated plainly, because it bounds what the restart half
of this design can do until crash-only trust lands
([#15894](https://github.com/kirodotdev/KiroCrew/pull/15894)): a resumed turn
that needs a tool approval waits for a person at that card like any
interactive turn, and when nobody comes the approval times out into a row the
error pass never retries. The boot pass therefore finishes on its own only the
turns that need no approval (or that the global override covers); for a
Trusted unattended chat it spends one turn to reach the approval card and
tells the person why (the notice below). The error pass is unaffected: a turn
that died mid-flight keeps the mode the chat holds, so a Trusted chat resumed
after an error runs its tools without asking. The setting's warning list states
both halves in one bullet (§4.1); a single "waits at the first tool approval"
sentence would be false for the error pass.

That the error pass keeps a held Trust is an accepted trade-off, not an
oversight, and this document records it as one. The pass changes a chat's
approval mode in neither direction: it restores no Trust a restart dropped
(above), and it downgrades none the person holds — a Trust picked in the
composer is the person's standing instruction for that chat, and a connection
that dropped or a session that was busy is not new information about whether
they still mean it, any more than it would be for the Resume press the pass
stands in for. Skipping, or resuming in Ask, a chat that holds a Trust grant
would make the error pass useless for exactly the chats most likely to be
running unattended, and would spend one of the person's grants without their
choosing, the mirror image of what §4.6 refuses to do with a dropped one. What
bounds the risk is what bounds every automatic resume: the budget (§4.4), the
resume body's instruction to check the current state before repeating any step
(§4.3), the retryable list closed to structural faults (§4.2), and the warning
the person read before turning the switch on (§4.1). A pin test,
`test_the_error_pass_neither_drops_nor_grants_trust` in
`test_restart_resume_permissions.py`, holds the pass to both halves: the flags
before equal the flags after, and `continue_slot_turn` receives no mode. The
alternative is listed, and rejected, in §6.

This design records and tells, and changes nothing else:

1. **A record of what the person last saw.** The metadata codec gains one
   slot-owned key, `approval_mode_seen` (`"trust"` | `"trust_reads"`), written by
   every save from the live flags — the periodic flush is "what the person last
   saw the chat hold". The full-line save omits the default (`normal`), so a
   chat nobody picked a mode for carries nothing extra and the existing metadata
   contracts are byte-identical; the merge form, which cannot delete a key,
   writes `normal` so a revoke overwrites an earlier `trust`. `yolo` is
   process-global and never a value here. A pick on an idle chat marks the slot
   dirty so the record follows it at the next flush (the crewmate seed does not:
   re-opening a bound thread is a no-op, and the seeded mode reaches the line
   with the thread's next save). The key is read back into
   `_approval_mode_recorded` **only**: no reader touches `_trust` /
   `_trust_reads`, the pinned `test_trust_flags_not_restored` is unchanged, and
   a new test pins that the recorded value is never applied.
2. **One notice when the mode changed.** After both restores, with the switch
   on, the restore compares each slot's recorded mode with the mode it came
   back holding. When
   they differ (and the recorded one was not already `normal`), one
   assistant-role system notice lands (`meta.kind = "approval_mode_dropped"`):
   *"This chat's permissions changed when the app restarted: it was on
   Trust (tools run without asking) and is now on Ask every time. To turn it
   back on, choose Trust in Approval mode under the message box."* — the remedy
   names the control as the screen labels it, not the codebase's "composer" (UX
   review) — on a crewmate thread,
   whose composer has no approval picker, the last sentence reads *"Open this
   chat from the Mates page to re-apply the mode its profile declares; set that
   mode in the profile to keep it across restarts."* It is a live append, like
   the pause notice's: the boot task runs after the gateway is listening, so a
   tab that reconnected first is watching, and a replay-mode append would have
   stored the row for the next transcript reload while the resumed turn — and
   its approval card — arrived live ahead of it, unexplained (Opus review;
   `test_the_dropped_mode_notice_is_broadcast_live_like_the_pause_notice`, red
   on the replay-mode append). A system notice, so every
   last-real-message scan looks through it and the Resume button is unaffected;
   idempotent, so a second restart before the person acts adds nothing. This
   runs **before** the boot pass, so no automatic resume runs a turn under a
   mode the person has not been told about.
3. **The declared grants keep working as they do.** A crewmate's declared
   `agents.<name>.approval_mode` is seeded when its thread is opened and, since
   [#23010](https://github.com/kirodotdev/KiroCrew/pull/23010), as each of its
   turns is admitted (`crewmate_approval.seed_for_turn`, before the provider is
   claimed); the global `agent.dangerously_skip_permissions` is re-established
   by `_apply_startup_yolo` before the restore; neither is touched. No notice
   lands where nothing changed or where the grant comes back by its own
   mechanism with no person to act: a chat whose mode is already back
   (crash-only trust when
   [#15894](https://github.com/kirodotdev/KiroCrew/pull/15894) lands); a
   crewmate's chat — its own thread, a conductor chat, or a worker it opened —
   whose recorded mode is the one its profile declares (an unset mode declares
   Trust), because the admission seed re-applies it before any turn runs — the
   turn the boot pass resumes included, so that turn runs under the mode the
   person last saw. The pass decides that the way the seed does, from the
   chat's execution record (`crewmate_name_for`), read off the loop for the
   chats with something to say, since nothing on a worker's slot says whose it
   is (Opus review, rev 56 and rev 57: an earlier revision told the thread
   "now on Ask every time" on the premise that the seed ran only on open, which
   #23010 had made false, and the first correction keyed the exemption on the
   thread's slot shape, so a worker was told;
   `test_a_crewmate_thread_the_boot_pass_may_resume_is_not_told_either` and
   `test_the_pass_decides_the_exemption_from_the_execution_record`, each red
   on the code before it); a chat that held a grant its profile does *not*
   declare (an approval card's "trust this session"), whose crewmate record is
   gone, or whose session record cannot be read (no turn is admitted, so
   nothing seeds), is always told; an app-owned chat, whose app
   owns the grant and which neither pass resumes (§4.2); and any chat while
   the global override is live, since under it nothing asks.

The notice is behind the switch like the passes: a gateway that has not opted
into automatic resumes is told nothing at restart, and its chats come back
exactly as they do today. Whether the notice should reach every gateway — a
chat silently losing its Trust at a restart is a defect whether or not anyone
pays for automatic resumes, and the row costs nothing — is a product-shape
decision this document does not take (§9 Q6). The record is kept either way, so
taking it later needs no migration: the first boot after the decision compares
honestly. The record is not gated by the switch on purpose: it is a record of
what the person last saw, and it is honest only if it tracks every pick and
revoke. A writer that paused while the switch was off would leave the last
grant on the line through the merge form (a merge cannot delete a key, which is
why it writes `normal` on a revoke), and the first boot after the switch came
back on would then tell the person their Trust was dropped when they had
revoked it themselves. The full-line writers omit the key for every chat that
never held a grant and write one short value for one that did; the merge form
cannot omit a key, so it writes `normal` there, and a slot line saved through
it carries the key whether or not the switch is on (the cost §9 Q6 names).
Nothing reads it but the notice.

What this deliberately does not do: seed anything itself. Re-seeding a
crewmate's declared mode on a boot-restored DM thread before resuming it, which
an earlier revision left to maintainers as a follow-up (§9 Q3), is main's own
behaviour since [#23010](https://github.com/kirodotdev/KiroCrew/pull/23010):
turn admission applies the Profile's mode to the conductor chat before any turn
claims a provider, person present or not. This document relies on that seed
and adds no second one.

### 4.7 Audit

Every automatic dispatch writes the `dashboard_continue` SEL line
`continue_slot_turn` already writes, with `via` set to `restart_resume` or
`error_resume`, so the trail distinguishes an automatic resume from a press
(no `via`) and from a `session_retry` tool call (`session_control`). The
budget-exhausted notice writes one `api_access` line with
`operation = "restart_resume:budget_exhausted"`, and the permissions notice
one with `operation = "restart_resume:approval_mode_dropped"` and the
`from->to` pair as its outcome, under `caller = "dashboard:restart_resume"`.

### 4.8 Surfaces

- **Transcript:** the `RecoveryCard` title and detail in §4.3 (new i18n keys
  under `pages.chat.recoveryCard`, fanned out to every locale); the exhaustion
  notice in §4.4 and the permissions notice in §4.6, both gateway-authored
  assistant-role system notices drawn by the existing `NoticeCard` (their two
  kinds join `SYSTEM_NOTICE_KINDS` and its frontend twin).
- **The row an automatic resume follows.** The terminal row the gateway resumed
  on its own — the restart row, the ladder's give-up row, a `retry_invited`
  row — still tells the reader to resume or retry, in red, right above the
  *Resumed automatically* card that says the turn was. Once that card follows
  it (`automaticResumeFollows`: the next row that bears on the turn is an
  `auto_restart` / `auto_error` continuation, never inferred from the missing
  Resume button, which a row the person typed past lacks too), `ErrorCard`
  states the row as settled history in a neutral tone, keeping the cause and
  dropping the instruction — *"This turn was interrupted when the app
  restarted. Its partial output is above."*, *"The connection was unstable."*
  — through `settledProse` (new keys under `pages.chat.errorCard`); the card
  under it says the rest, so one fact is said once. The restart row alone keeps
  a pointer at the partial output, because its wire text had one and that
  output may be work the resumed turn repeats (the switch's first warning); the
  "please retry" rows never pointed anywhere, so their settled forms add
  nothing (UX review, a later round). A person's own Resume
  press is deliberately not a case: the row it follows keeps the rendering it
  has always had, so a dashboard that never turns the switch on reads exactly
  as before. A resumed row whose words carry no instruction stays verbatim.
- **Settings → Chat:** §4.1; the command-palette settings registry is
  regenerated (`npm run gen:settings`).
- **Sidebar:** unchanged. The slot summary's `interrupted` field reads the
  transcript as it does today, so a chat whose resume is armed but not yet
  dispatched still shows as interrupted until the inject row lands.
- **Channels:** a chat linked to a channel gets the exhaustion notification
  through `state.notify`'s existing fan-out; nothing channel-specific is
  added.

## 5. Security considerations

- **No new authority.** The automation calls `continue_slot_turn` with
  `directive_user_origin=False` and an empty `request_app`/`cron_creator`
  identity, so the queued entry carries no authenticated-human flag, the
  resumed turn runs under the slot's current approval mode, and every refusal
  the button honours is honoured. An app token or a cron cannot reach the
  pass; it is not a route.
- **Nothing an agent writes can arm, extend or escalate it.** The switch and
  its two numbers live in `config.json`, which agent file tools refuse. The
  one persisted datum the automation reads back, `auto_resume_stamps`, lives
  on the agent-writable metadata line — deliberately, because its worst
  forgery is bounded: an agent that deletes its own stamps buys itself more
  retries of *its own chat*, spending tokens inside a window the operator
  sized; an agent that forges a full list stops its own retries. Neither
  grants a tool, changes a mode, or reaches another chat. The read fails
  closed on anything malformed. This is the same trust boundary the turn
  marker already draws for `turn_in_flight_generation`.
- **Approval modes.** §4.6 introduces one persisted *record* of a mode and no
  persisted grant: the record is written from the live flags and read back
  only to word a notice. A forged `approval_mode_seen` on the agent-writable
  metadata line can make the chat print a notice about a change that did not
  happen; it cannot grant, extend or restore anything.
- **Spend.** Bounded per chat by the budget, globally by `budget × chats`,
  and in time by the window; the default (5 per hour) is one automatic resume
  per twelve minutes per chat at most.
- **Audit.** §4.7; no decision is taken without a SEL line.

## 6. Alternatives considered

- **An autonudge loop armed by the user.** `monitor_start` loops survive
  restarts and could re-drive a session, but a loop is a scheduled *message*,
  not a continuation: it opens a new turn, it needs the person to arm it per
  chat before leaving, and it carries no notion of "the previous turn was cut
  off". It also stalls on exactly the approval problem of §4.6
  (#12275 step 4). The design borrows its persistence idea (a durable record
  bounds a restart-surviving automation) and nothing else.
- **A frontend-driven auto-press.** The dashboard could press Resume when it
  sees the restart row. It would resume only chats a browser has open, only
  while a browser is open, and once per browser — the wrong shape for
  unattended work, and unauditable from the gateway's side.
- **Resuming only chats that declared unattended intent** (an armed loop, an
  open conductor goal, a worker bound to one), as
  [#13838](https://github.com/kirodotdev/KiroCrew/issues/13838) proposes. A
  reasonable first slice, but the reported loss is a person's own chat with a
  day's work in it, which declares nothing. The switch is the declaration:
  off by default, one sentence of help about what it spends. A per-chat
  opt-out is §9 Q2.
- **Retrying every terminal error.** Rejected for the kinds in §3: a retry
  that cannot cure the fault spends the budget to reproduce the error row.
- **No budget, only backoff.** Rejected: a gateway that crashes on the resumed
  turn would resume it on every boot forever.
- **Skipping, or resuming in Ask, a Trusted chat on the error pass.** Rejected
  (§4.6): it would silently downgrade a mode the person picked, on information
  (a dropped connection, a busy session) that says nothing about whether they
  still mean it, and would exclude exactly the unattended chats the error pass
  is for. The person is told, in the switch's warning, that an error resume
  keeps the chat's Trust, and the budget bounds what it can spend.

## 7. Rollout

One pull request carries this document and the code; the switch is off by
default, so merging it changes nothing for anyone who does not turn it on.

- **Backend.** `src/kiro_crew/dashboard/restart_resume.py` (boot pass, error
  pass, classifier, backoff, budget, the two notices); the `body` argument and
  `via` values on `continue_slot_turn`; the three config keys with bounds and
  `_meta`, exposed on `GET`/`PUT /api/dashboard/config`; the metadata-codec
  rows for `auto_resume_stamps` and `approval_mode_seen` (slot-owned, both save
  forms, round-trip pinned); the two recovery prefixes and the two bodies
  registered with the drift guard; the two system-notice kinds; the restore
  hook, the turn-end hook, the budget reset on a person's send and Resume, the
  cancel on Stop and close.
- **Dashboard.** Settings → Chat controls, the `RecoveryCard` prefixes and
  copy, the system-notice twin, i18n keys in every locale, the regenerated
  settings registry.
- **Tests.** The classifier table (every row of §4.2, both directions), budget
  arithmetic at the window edge, stamps surviving a simulated restart and
  failing closed on a malformed line, the reset on a person's action, the boot
  pass skipping a chat the person touched first, the error pass's task
  cancelled by a send, no dispatch for any never-retry kind, the notice landing
  once and restoring nothing, an end-to-end run through the real persistence
  path, the endpoint round trip, and `test_trust_flags_not_restored` left
  exactly as it is.
- **Docs.** [history.md](../system-specs/modules/history.md) (the restore path
  gains a step), [config.md](../system-specs/modules/config.md) (the three
  keys), [error-handling.md](../system-specs/common/error-handling.md) (the
  closed retry list and the tag-at-the-writer rule, so a later "please
  retry" row is not left out of retries unknowingly), and the row in this
  directory's README.

## 8. Related

- [#13838](https://github.com/kirodotdev/KiroCrew/issues/13838) — platform-side
  resume for turns cut by a restart (this design, widened from unattended-only).
- [#12275](https://github.com/kirodotdev/KiroCrew/issues/12275) — per-chat trust
  dies on restart (§4.6 ships its fallback half behind the switch: the chat is
  told, in the chat, when its mode changed).
- [#4756](https://github.com/kirodotdev/KiroCrew/issues/4756) — auto-continue
  after the turn ceiling (explicitly out of scope; the budget and the body
  prefix here are reusable if that decision is taken).
- [#13853](https://github.com/kirodotdev/KiroCrew/issues/13853) — notification
  when a turn dies (the exhaustion notice is a partial answer; a notice on the
  first death is not proposed here).
- [rfc-crash-only-session-trust.md](rfc-crash-only-session-trust.md) /
  [#15894](https://github.com/kirodotdev/KiroCrew/pull/15894) — the one
  restart-surviving ad-hoc grant under design; §4.6 step 3 composes with it.
- [rfc-crewmate-profile-settings.md](rfc-crewmate-profile-settings.md) — the
  declared crewmate approval mode, which this design leaves where it is.

## 9. Open questions and decisions requested

1. **Default of the switch.** This document proposes off. The argument for on
   is that a restart is not a user action and resuming is what the user would
   have done; the argument for off is that it spends turns with nobody
   watching and that the error pass retries a *generic* error row. Proposed:
   off, revisit after a release of opt-in use.
2. **Per-chat opt-out.** Should a chat be able to decline automatic resume
   (a session directive, or the Stop card's presence) beyond the Stop card
   that already excludes it? Proposed: not in v1; the global switch and the
   Stop card are enough to start.
3. **Ad-hoc per-chat Trust across an owner-chosen restart.** The question
   behind this document, and the one
   [#12275](https://github.com/kirodotdev/KiroCrew/issues/12275) has waited on
   since September. This document does **not** propose it, because the review
   rule forbids it; it ships the notice instead. If maintainers want the
   restore, the shape is known: a gateway-only record in a masked leaf (the
   `vouched-executions` / `tag-grants` precedent), the governance verdict
   re-read at restore, `yolo` never, incognito never, a SEL `mode_change`
   marked as a restore — and the rule, the pinned
   `test_trust_flags_not_restored` and the picker copy all change together.
   Proposed: decide it on #12275, not here. The smaller, rule-compliant
   follow-up an earlier revision named here — re-seed a crewmate's **declared**
   `approval_mode` on a boot-restored DM thread before the boot pass resumes it
   — landed on main as [#23010](https://github.com/kirodotdev/KiroCrew/pull/23010)
   (`crewmate_approval.seed_for_turn`, at turn admission), so a boot-resumed
   crewmate thread already runs under its declared mode and §4.6 tells it
   nothing; nothing is left to decide for it here.
4. **A gateway-level storm guard.** Should a boot that follows N boots in M
   minutes skip the pass entirely, independent of per-chat budgets? Proposed:
   not in v1 (§4.5).
5. **The generic-error row.** Should an `error` row with no structural kind be
   retried, or should the retryable list be strictly positive? Resolved:
   strictly positive. The rows that made the generic error worth retrying are
   the runner's own "please retry" rows, and those now carry `retry_invited`
   from their writers, so the list names kinds only and reads no wording; an
   unclassified terminal error is not retried until someone tags its writer.
6. **The permissions notice for a gateway that leaves the switch off.** §4.6's
   notice is behind the switch, so a gateway that never opts into automatic
   resumes is not told when a restart dropped a chat's Trust — the same silence
   as today. The argument for an always-on notice is that the silence is a
   defect in its own right and the row costs nothing; the argument against is
   that it changes what every restored chat shows for every person who held a
   Trust, without their choosing. Proposed: behind the switch in v1; the
   always-on form is a decision for here or for
   [#12275](https://github.com/kirodotdev/KiroCrew/issues/12275), and needs no
   migration when taken (the record is kept either way). The cost of keeping
   it either way (Design review): `approval_mode_seen` is written whether or
   not the switch is on, so a gateway that never opts in still gains the key
   on its slot lines — on every line the merge form saves (`normal` when no
   grant was held, since a merge cannot omit a key), and on a full-line save
   only for a chat that held a Trust. One short string per line, read by
   nothing but the notice, and reversible by deleting the key; §4.6 gives the
   reason it is not gated.
