---
title: Restart resume — pick an interrupted turn back up without a person, within a budget
status: draft
author: hickoxd
created: 2026-10-14
last-audited: 2026-10-15
audited-at: 213339f671
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
mode the person last saw the chat hold and, when the chat comes back holding a
different one, lands one status row saying so and naming the remedy (§4.6).
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
  `src/kiro_crew/dashboard/state.py`) and waits.
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
  (`src/kiro_crew/dashboard/handlers/members.py`) — but only from the
  open-thread route, so a DM thread the boot restore rebuilt sits in
  interactive mode until someone opens it from the Crewmates page. Nothing
  records which mode a chat held, so nothing can say it changed.

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
  answered by its notice, and a chat the user closed
  (`chat_close_interruption`) are deliberate endings, exactly as
  `is_turn_interrupted` and `continue_slot_turn` already treat them.
- Relay (remote crew) slots, archives, incognito and temporary sessions: their
  turns are not this gateway's to resume, or nothing of them persists.
- Any change to the in-turn retry ladders. They stay as they are; this design
  starts where they give up.

## 4. Design

### 4.1 Settings

Three keys on `DashboardConfig` (`src/kiro_crew/config/sections.py`), each with
`_meta` label and help, read through the section builder like their
neighbours, hot-applied (no restart; the boot pass reads the config the boot
already loads, the error-time pass reads the live snapshot):

| Key | Type / default | Meaning |
|---|---|---|
| `dashboard.resume_interrupted_turns` | bool, **false** | The switch over both passes. Off: no automatic resume is armed or dispatched. (The record of what the person last saw and the notice in §4.6 are not behind it.) |
| `dashboard.resume_retry_limit` | int, **5** (1–20) | Automatic resumes allowed per chat inside the window, restart and error resumes counted together. |
| `dashboard.resume_retry_window_minutes` | int, **60** (5–1440) | The rolling window the limit is counted over. |

Settings → Chat gets a `SettingsToggle` for the switch directly under
*Restore Sessions*, and — only while it is on — a stepper for the limit and a
select for the window, the same nesting *Restore Sessions* uses for *Restore
Window*. The help text of the switch says, in the first sentence, that it
spends model turns and tool calls with nobody watching, so the default is off,
and in its last sentence what it does **not** do: a Trust picked in the
composer still ends at a restart, and the chat says so when that happens
(§4.6) — the sentence the Trust picker is missing today (see
[#12275](https://github.com/kirodotdev/KiroCrew/issues/12275)).

The switch does not require `restore_sessions`: foldered and pinned chats are
restored regardless, and a pinned chat is the likeliest one to hold a day's
work.

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
| `error` row with **no** structural kind | yes, with backoff | the generic terminal error (an `AcpError` the runner could not classify); bounded by the budget like every other |
| `turn_timeout`, `auth_required`, `model_unentitled`, `usage_limit`, `context_overflow`, `app_agent_refused`, `chat_close_interruption` | **no** | §3 non-goals: a retry cannot cure it, or the user ended it, or another decision owns it |
| an approval prompt nobody answered or that had no time left, "Session stuck", a turn that failed before it started (`meta.turn_failed`) | **no** | untagged rows a retry cannot cure; matched on the fixed words their writers use (`NEVER_RETRY_TEXT_PREFIXES`, `TURN_FAILED_META`) |
| a Stop card anywhere in the newest turn, a `/compact` answered by its notice | **no** | the person's recorded intent that nothing continues on its own — including the give-up row the ladder writes when a Stop lands during its backoff |

The retryable list is a frozenset in `restart_resume.py`
(`RETRYABLE_ERROR_KINDS`; the ladder's give-up row carries `meta.notice`
rather than `meta.kind` and is handled beside it), so a new terminal kind is
**not** retried until someone adds it — fail closed, the direction every other
authorization list in the gateway takes.

Every candidate is then subject to `continue_slot_turn`'s own refusals,
unchanged: running, stopping, queue pending, approval pending, sub-agents
running, empty, `session_start_repeat`. A refusal is logged at `info` and
costs nothing from the budget — nothing was dispatched.

### 4.3 Mechanism

One module, `src/kiro_crew/dashboard/restart_resume.py`, with two entry
points and no state of its own beyond what the slot carries.

**The boot pass.** `_restore_dashboard_sessions` gains one call after both
restores, the counter reseed and the permissions notice (§4.6):
`restart_resume.start_boot_pass(state, cfg)`, which runs
`resume_after_restart` as a background task so the server's listen is not
delayed by it. The pass looks only at slots whose metadata line carried the
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
`chat_runner.py`, once the slot is idle) gains one call,
`restart_resume.schedule_after_error(state, slot)`. It reads the tail through
the same classifier the boot pass uses, declines while the switch is off in the
config snapshot the process holds, and otherwise arms a per-slot `asyncio`
task that consults the budget (§4.4), sleeps the backoff, reads the switch
again (turning it off during the wait stops the retry), and then calls
`continue_slot_turn(..., via="error_resume")`. The task handle is stored on
the slot (`_auto_resume_task`) and cancelled by anything that makes it moot: a
person's send, a person's Resume, a Stop, a close. Each of those also moves a
per-slot epoch that the dispatch re-reads under the slot lock right before its
queue insert, so a cancel that lands after the backoff but before the insert
refuses the dispatch rather than leaving a gateway-authored entry at the head
of the queue. There is never more than one armed per slot.

**What the model reads.** Two bodies with their own prefix lines, `[Continue
— automatic after a restart]` and `[Continue — automatic after an error]`,
registered beside `MANUAL_RESUME_RECOVERY_PREFIX` in `state.py` and in the
`RecoveryCard` prefix table. The shared text tells the model that the gateway
resumed the turn and that no person has looked at the partial output above —
so a model that was about to ask the user something does not read a press that
never happened as an answer — and that it must check the current state before
repeating a step. `continue_slot_turn` takes the body as an optional argument;
the button and the tools are unchanged. Both bodies join the runner's
interrupted-turn set (`_INTERRUPTED_TURN_RESUME_MSGS` in `chat_utils`, which
replaces the literal of §2), so on a natively resumed session the lost turn is
re-injected for an automatic resume exactly as for a press; the runner leaves
the gateway's own assistant-role system notices (§4.6's among them) out of the
rows it hands the builder, since they sit in the span it quotes as the model's
partial answer and are not the model's words.

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
  resume on every boot; the stamp is appended by the dispatcher's
  `before_dispatch` hook, under the slot lock right before the queue insert,
  so it rides the awaited save that marks the resumed turn in flight and a
  gateway that dies on that turn before the next 5 s flush still counts it.
  The read is validated like the turn marker's: not a list, more than 64
  entries, a non-finite or future stamp → the list is treated as **full** (64
  stamps; fail closed for the automation; the manual Resume is untouched), a
  warning names the slot, and the pause notice says the record could not be
  read rather than quoting the sentinel count — the sentinel is told apart by
  its size (the gateway never appends past the limit, whose ceiling of 20 is
  far below 64), so a limit the person lowered below the stamps already in the
  window is quoted as it is;
- **reset** by a person's own action on the chat — a typed message, a Resume
  press — on the same principle `session_start_failure_streak` uses: new
  information from a person starts the count over. Neither an automatic resume
  nor an agent's `session_retry` / `session_continue` (which carry no human
  origin) resets it: the budget bounds exactly what no person asked for.

On exhaustion the chat gets one assistant-role system notice
(`meta.kind = "auto_resume_paused"`, drawn by the existing `NoticeCard`),
*"⚠️ Automatic resume paused for this chat: 5 attempts in the last 60 minutes
(the limit is 5). Press Resume to continue; that also resets the count."*,
one `state.notify("agent", ...)` so the notification centre and a linked
channel hear it, and the Resume button, which was never removed. The notice is
idempotent per budget: nothing is added while a pause notice already stands
with no automatic resume after it in the transcript, so a crash loop that
reaches exhaustion on every boot, or an unattended turn that fails again while
the budget is spent, does not stack rows; a new notice is warranted only once
the automation has resumed again and spent the budget again. The transcript
then reads exactly as it does today.

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
today nothing says so.

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
2. **One notice when the mode changed.** After both restores, the restore
   compares each slot's recorded mode with the mode it came back holding. When
   they differ (and the recorded one was not already `normal`), one
   assistant-role system notice lands (`meta.kind = "approval_mode_dropped"`):
   *"🔒 This chat's permissions changed when the app restarted: it was on
   Trust (tools run without asking) and is now on Ask every time. A permission
   granted to this chat lasts until the app is stopped or restarted. Pick it
   again from the composer if you still want it."* — on a crewmate thread,
   whose composer has no approval picker, the last sentence reads *"Opening it
   from the Crewmates page re-applies the mode its profile declares; set that
   mode in the profile to keep it across restarts."* A system notice, so every
   last-real-message scan looks through it and the Resume button is unaffected;
   idempotent, so a second restart before the person acts adds nothing. This
   runs **before** the boot pass, so no automatic resume runs a turn under a
   mode the person has not been told about.
3. **The declared grants keep working as they do.** A crewmate's declared
   `agents.<name>.approval_mode` is seeded when its thread is opened, and the
   global `agent.dangerously_skip_permissions` is re-established by
   `_apply_startup_yolo` before the restore; neither is touched. No notice
   lands where nothing changed or where the grant comes back by its own
   mechanism with no person to act: a chat whose mode is already back
   (crash-only trust when
   [#15894](https://github.com/kirodotdev/KiroCrew/pull/15894) lands); a
   crewmate thread whose recorded mode is the one its profile declares (an
   unset mode declares Trust), because `_seed_member_approval` re-applies it
   when the thread is opened — *unless* the boot pass may resume that thread
   first (the switch is on and the restart cut its turn), since the resumed
   turn would run under the mode the thread holds now, which step 2's rule
   forbids running untold; a thread that held a grant its profile does *not*
   declare (an approval card's "trust this session"), or whose crewmate record
   is gone (nothing seeds then), is always told; an app-owned chat, whose app
   owns the grant and which neither pass resumes (§4.2); and any chat while
   the global override is live, since under it nothing asks.

What this deliberately does not do: re-seed a crewmate's declared mode on a
boot-restored DM thread before resuming it. That would be rule-compliant (a
declared grant), but it is two decisions — re-apply Trust, then run a turn with
nobody present — and the first should stay where it is made today, on the
Crewmates page. It is a one-line follow-up if maintainers want it (§9 Q3).

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
  keys), and the row in this directory's README.

## 8. Related

- [#13838](https://github.com/kirodotdev/KiroCrew/issues/13838) — platform-side
  resume for turns cut by a restart (this design, widened from unattended-only).
- [#12275](https://github.com/kirodotdev/KiroCrew/issues/12275) — per-chat trust
  dies on restart (§4.6 ships its fallback half: the chat is told, in the
  chat, when its mode changed).
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
   Proposed: decide it on #12275, not here. A smaller, rule-compliant
   follow-up is also available: re-seed a crewmate's **declared**
   `approval_mode` on a boot-restored DM thread before the boot pass resumes it
   (today the seed runs only when the thread is opened from the Crewmates
   page). Not done here for the reason §4.6 gives.
4. **A gateway-level storm guard.** Should a boot that follows N boots in M
   minutes skip the pass entirely, independent of per-chat budgets? Proposed:
   not in v1 (§4.5).
5. **The generic-error row.** Should an `error` row with no structural kind be
   retried (this document says yes, within the budget), or should the
   retryable list be strictly positive — only `transient_give_up`,
   `session_start_failed` and the restart row? Proposed: yes, because the
   unclassified terminal error is the common shape of "the backend went away
   for a minute", and the budget bounds the cost of being wrong.
