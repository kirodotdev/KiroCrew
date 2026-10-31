---
title: Return-triggered warm task brief — replace the immediate warm resume
status: accepted
author: zejiangg
created: 2026-10-14
last-audited: 2026-10-14
audited-at: d06b611f549d
doc-pr: 20157
implementation-prs: []
tracking-issues: [20156, 25306]
supersedes: []
superseded-by: []
---

# RFC: Return-triggered warm task brief — replace the immediate warm resume

- Status: `accepted`. Maintainer `iamwhatever` [accepted the direction and explicit-signal false-negative tradeoff](https://github.com/kirodotdev/KiroCrew/pull/20157#issuecomment-6167061835) on 2026-10-14: cold welcome stays unchanged; the user must leave, stay away for at least 30 minutes, and return; the return shows deterministic status before one model call; stale or cancelled work clears that status without publishing its result.
- UX amendment, 2026-10-15: the deterministic preparation and generated summary render as two ordinary crewmate messages, not a card, widget, status banner, or special result row. The second message has **Problem**, **Current status**, and **Suggested actions**. This supersedes the transient-status, unattributed-notice, and silent-invalid-output clauses below.
- UX amendment, 2026-10-16: the first message is a fixed warm opener in first person ("Welcome back, let me catch you up on where we are."), in the script of the user's request; the second is one first-person prose message from the model saying what was asked, where it stands, and one to three suggestions, with no **Problem**/**Current status**/**Suggested actions** headings or lists. This supersedes the three-part shape and gateway-supplied quote of the 2026-10-15 amendment and section 3.5; trigger rules are unchanged.
- Trigger amendment, 2026-10-18: the shipped trigger never fired for the owner. Since #20422 merged, no transcript holds a single claim row. The owner's real return left the crewmate thread for another chat, hid the page there, and opened the crewmate later; section 3.2 counts none of that as away. The amended rules: (1) leaving a thread for another thread starts its away episode, like a hidden page; (2) an episode ends only when an owner frame names that thread again, so frames for other threads, windows, or devices no longer wipe it; (3) a return while the thread runs a turn waits for that turn to end, up to 30 minutes, while the owner stays on the thread, instead of being skipped; (4) a prepared result goes stale only on a new user turn, a new away episode, a reset or replaced slot, or a finished task. Patrol turns, `[work-ledger wake]` rows, and worker reports still change the dedup fingerprint, but they no longer drop a prepared result. A turn running at publication defers the result until it ends. (5) Only a live row the person typed cancels a brief in preparation. Assistant rows, automation rows such as a wake, and turn admission do not. The expected rate is one brief per qualifying return. Patrol cadence adds none; it only makes nearly every such return eligible. The ceiling is 48 per thread per day. The owner's conductor thread on 2026-10-17 had about four returns after 30 minutes or more away, so about four background calls a day. The 30-minute minimum, durable claim, fingerprint dedup, one model call per claim, and the explicit-hidden-signal false negatives are unchanged. This supersedes the "Switch chats", "Hide on X, return on Y", and "worker/task change" rows below.
- Trigger amendment, 2026-10-22: away is time off screen, not a lost leave signal. The 2026-10-18 rules still missed every return after a dropped hide frame or a gateway restart, because the away episode lived only in memory. The amended rules: (1) the gateway keeps, in memory, when each thread was last on screen in any owner view: stamped by an owner frame naming it, an owner frame leaving it, and the close of an owner socket that showed it; (2) an owner frame naming thread X is a return only when no owner view already shows X, so watching a thread, a re-sent frame, and a second window are never returns; (3) away time runs from the latest of that last-on-screen time, the owner's last genuine message, and the last brief claim, the two durable times read from the transcript with naive rows from older builds read as local time; (4) when the process has no on-screen record of X (after a restart, or after the bounded record evicts it), only the durable times count; (5) the frame a socket sends on first connect or reconnect carries `announce: true`, and an announce for a thread with no on-screen record marks it seen now and never briefs, so the reconnect wave after a restart cannot fire a burst; an announce after a recorded absence (a page reloaded after a real leave) still qualifies. A restart while away followed by the tab turning visible is a non-announce frame and briefs from the durable times. Accepted false negatives: a brand-new tab opened after a restart, and a reconnect after sleep, give no brief. The 30-minute minimum, durable claim, fingerprint dedup, one model call per claim, deferral, and stale rules are unchanged. This supersedes the in-memory away episode in sections 3.2 and 3.3 and the "Gateway restarts during away period" row.
- UX amendment, 2026-10-24: the owner found the four-field card too thin to act on. The brief now reads like a manager's report to the owner who hired them: a status and headline first, then what needs the owner, then how far it is from the goal, then what they asked for. Progress (`accepted of total` work items) is counted by the gateway from the ledger, never by the model. The model may add up to two sections of its own for anything else the owner must decide. Section 5.1 holds the shape. Trigger, preparation, spend, staleness and reader-filter rules are unchanged.
- Leave amendment, 2026-10-26: the owner left the thread a few seconds after the opener appeared and came back to the opener alone; the card never arrived. Leaving is a pause, not a cancel. Once the opener is posted, hiding the tab or switching to another chat no longer drops the brief: the result is ordinary history and lands while the owner is away, and a turn running at publication still defers it. Only a typed row, a reset, a close or a replaced slot cancels a brief past the opener, and a new user turn still makes its result stale. Leaving before the opener still drops the attempt, as before. A return while the brief is in flight starts no second one, so the owner sees exactly one card. This supersedes "leaves" in the section 3.4 stale-result table, the foreground recheck before publication in section 3.7, and "a second leave" in the race tests.
- UX amendment, 2026-10-26: the report read too stiff. The prompt now names the facts the owner must come away with (what needs them and what it unblocks, the ask, distance to the goal, what is next) and lets the model choose tone and layout in a free-form `body`, plus an optional `widget` drawn in the chat widget sandbox. Section 5.1 holds the shape.
- Writer amendment, 2026-10-28: the brief read only the ledger snapshot, so it reported a merged or conflicting pull request as still open. On a qualifying return the gateway now wakes the crewmate for one read-only, background-priority turn. The crewmate checks the live state of its open tasks with its own tools, updates its records and Dashboard fields, and writes the brief itself. The one-call brief stays as the fallback. Trigger, opener, progress counting, staleness and reader-filter rules are unchanged. Section 3.8 holds the design.
- Scope amendment, 2026-10-29 (owner decision, implemented in [#25881](https://github.com/kirodotdev/KiroCrew/pull/25881), issue [#25838](https://github.com/kirodotdev/KiroCrew/issues/25838)): a Crew Mode chat gets the same brief. A Crew Mode root (a root dashboard chat running a conductor, `crew_mode.slot_is_crew_mode_root`) is eligible beside a crewmate thread; its workers and every other chat are not. One trigger rule is stricter: a Crew Mode chat can stay visible in a split-view pane while another pane holds focus, so its away episode starts only when the page sends an explicit hide (`slot_focused` with `slot: null, hidden: true`, which the client sends when the slot it last announced is a Crew Mode chat on a chat route, as it does for a mounted crewmate thread). The hide marks that one owner view, and only while no other owner view shows the chat; that view's next frame spends the mark, so the return must name the chat the hide left; a reveal re-announces the focused split-view pane rather than the active chat for that reason. A pane or sidebar switch, a socket loss and a gateway restart never start one, so the durable-time fallback after a restart does not apply to Crew Mode. Away time, claim, fingerprint, one call per claim, opener, progress count, staleness and cancellation rules are unchanged. Spend is the crewmate's: at most one tool-free background call per qualifying return, under the same durable claim and the 48-per-thread daily ceiling. The only other difference is the prompt: it names the writer as the conductor and asks it to say which work items finished, which wait on the owner, and which are still running. The open-task check already reads the conductor goal, the work items and the running workers. Crewmates get no first-open brief, so neither does Crew Mode.
- UX amendment, 2026-10-31 (owner decision; implementation [#26500](https://github.com/kirodotdev/KiroCrew/pull/26500), issue [#26494](https://github.com/kirodotdev/KiroCrew/issues/26494)): the model-drawn `widget` drew its own progress ring against the prompt, cut long lines off at 320 px, and looked different on every return. The model now writes words only; it is not asked for a `widget`, and one it sends is ignored. The gateway lists what waits on the owner itself, from the ledger the snapshot already reads, and a fixed card component draws progress and that list as one picture. The owner chose this "glowing orb" direction from three mockups. Section 5.1 holds the shape. Trigger, preparation, spend, staleness, progress counting and reader-filter rules are unchanged; a stored brief keeps drawing its old `widget`. This supersedes the `widget` of the second 2026-10-26 UX amendment and of section 3.8.
- Redesign amendment, 2026-10-31 (owner decision; issue [#25306](https://github.com/kirodotdev/KiroCrew/issues/25306)): a study of every real brief the owner received found most of them stated stale or false facts and none carried the owner's pending choices. The brief is rebuilt around two rules: verify before stating, and make every ask usable from the message alone. It is one message, replaced in place from a "preparing" indicator; a quiet return is one line; only goal-scoped numbers are shown; scope stays crewmate threads and Crew Mode roots. The trigger is unchanged. Section 3.9 holds the design and lists what it supersedes.
- Audited on `d06b611f549d` (`main`, 2026-10-14).
- This amends only the **warm** branch of [rfc-crewmate-greeting.md](rfc-crewmate-greeting.md) and [rfc-crewmate-greeting-as-message.md](rfc-crewmate-greeting-as-message.md). Their cold-welcome decision remains fully in force.

## 1. Problem

The current warm resume appears as soon as a crewmate chat with an open task is opened. It is assembled from fixed client copy and work-ledger counts. It does not begin with the request that caused the work, and it can appear while a newer answer is about to land.

The useful moment is a real return: the user left Kiro Crew, stayed away long enough to lose task context, and came back. Only that return should spend a model call and welcome the user with the original request, current task truth, and the next move.

## 2. Current behavior, verified

When `useMateGreeting` opens a confirmed idle crewmate thread, it immediately reads `GET /api/crew-board`. An open work-ledger item produces `MateResumeCard`, rendered through `ChatPane.crewmateGreeting` as a host-drawn message after the transcript. It uses no model call and writes no transcript row. The same hook takes a separate cold branch only when no goal is in flight.

Kiro Crew already has an owner-gated foreground lifecycle. `attachFocusRelay` sends `slot_focused` over the existing authenticated WebSocket; first connect and reconnect re-announce it through `reconnectCatchUp`; a hidden tab sends `slot: null`; and the server stores one foreground slot per owner connection through `slot_retention.note_foreground`. The Crewmates page separately uses `document.hidden`, `document.hasFocus()`, `window` focus/blur, and `viewedThread` to register a member thread only while it is on screen. `slot_read` is not presence: `_handle_slot_read` is a pure unread-badge relay and stores no read state.

The current foreground seam does not yet express the full return contract. It re-announces `chat.activeSlot`, while a member thread lives in `viewedThread`; it sends blur for a hidden document but not every focus loss or page leave; and it stores no away start. The implementation extends this existing owner presence seam. It does not infer presence from message age, `slot_read`, a client clock, or an ambient “active” flag.

Before this RFC changed anything, focused tests passed on the audited base: 62 backend tests in `test_agent_welcome_message.py` plus `test_mate_welcome.py`, and 45 frontend tests in `mateGreeting.test.ts`, `useFirstGreeting.test.tsx`, and `ChatPane.scrollChrome.test.tsx`. Existing `MembersPage` and WebSocket tests also pin visible/focused `viewedThread` registration, hidden-tab retirement, first-connect focus, and reconnect focus re-announcement.

## 3. Decision

### 3.1 Warm only

Replace only the immediate warm resume. A crewmate chat with an active task no longer draws fixed task status merely because the thread opened.

> 2026-10-24: the cold Recap is removed; the warm task brief is the only return greeting.

Every cold-welcome path remains exactly as it is on the audited base:

- `useMateGreeting` still takes the cold branch only when no work-ledger goal is in flight.
- `COLD_AFTER_MS` remains six hours.
- `GET /api/members/{slug}/recap`, `MateWelcomeCard`, its copy, hide action, once-per-idle-stretch rule, failure notice, and user-visible placement remain unchanged.
- `useFirstGreeting`, `POST /api/members/{slug}/greet`, `dashboard.mate_welcome`, the `first_greeting` create flag, and Mate's first model welcome remain unchanged.
- Agent-spec `welcomeMessage` reading, dashboard display, KAS projection, bundled values, and agent-switch behavior remain unchanged.

This RFC does not remove a welcome from a new or cold chat. It changes only the active-task warm resume.

### 3.2 Exact trigger: leave, 30 minutes away, return

A delayed warm task brief is eligible only when all conditions hold in order:

1. The gateway receives `{type: "slot_focused", slot: null, hidden: true}` from the last owner connection whose foreground row names thread X.
2. Thread X stays in that in-memory away episode for at least 30 continuous minutes.
3. The first later owner `slot_focused` frame names thread X; a frame naming anything else ends the episode without a call.
4. On that return, thread X has an open task and a durable original user request.
5. No earlier warm-brief attempt claimed the same durable task fingerprint.

The **return frame** is the trigger. Thirty minutes passing is not a trigger. No periodic timer runs.

| State | Status or model call |
|---|---|
| Page remains visible for 30 minutes or longer | None |
| Page remains visible but window loses focus | None |
| Page remains hidden for 30 minutes or longer | None until a real return |
| User returns before 30 minutes | None |
| Focus event repeats while already foreground | None |
| Another tab already shows the thread | None |
| Switch chats and stay 30 minutes or longer | None |
| Open a non-chat route for 30 minutes or longer | None |
| Leave the New crewmate card open for 30 minutes or longer | None |
| Focus a split-view placeholder pane | None |
| Hide on thread X, return on Y, then open X later | None |
| User returns after 30 minutes, fingerprint already claimed | None |
| First eligible return, or return after task-state change | One claimed warm-brief attempt |

Thirty minutes is the accepted minimum. The implementation names it once as `WARM_TASK_BRIEF_AWAY_SECS = 30 * 60`. It is an away-duration check on a return edge, never a wake timer or repeat cadence.

### 3.3 Trusted presence and multiple tabs

The client extends the existing `slot_focused` frame with one optional reason bit, `hidden: true`. It is valid only with `slot: null`, and only the existing `visibilitychange` hidden branch sends it. The flag says why presence cleared; it supplies no time or duration.

- On a chat route, the presence sender continues to report `chat.activeSlot`.
- On the Crewmates route, a separate mounted-thread presence value reports the confirmed member thread while the document is visible, regardless of window focus. The existing focus-aware `viewedThread` value remains unchanged and serves unread badges only.
- First connect and reconnect use that mounted-thread value on the Crewmates route and `chat.activeSlot` elsewhere, but send no focus frame while `document.hidden` is true. The later `visibilitychange` return sends the real return frame.
- Window focus may re-announce the same slot. Window blur sends nothing. Chat switches, member-thread switches, non-chat routes, the New crewmate card, split-view placeholder focus, page teardown, and socket cleanup keep their existing `slot_focused` behavior but never add `hidden: true`.
- Frames keep the existing WebSocket origin, owner, and app-token checks. The server accepts no client-supplied time or duration.

`document.hidden` is also the sole desktop authority; no native Electron IPC is added. The dashboard's `WebContentsView` leaves `backgroundThrottling` at its default `true`. Electron's [Page Visibility contract](https://www.electronjs.org/docs/latest/api/browser-window#page-visibility) says minimized or explicitly hidden windows report hidden on every platform, and fully occluded windows report hidden on macOS. Therefore macOS minimize, hide-to-tray, and full occlusion can arm; Windows and Linux minimize or `win.hide()` can arm; partial occlusion, side-by-side windows, and unfocused documents that remain visible do not. If a compositor does not report hidden, no brief appears—the accepted explicit-signal false negative.

The implementation pins that controllable premise with an Electron source test that the dashboard view never sets `backgroundThrottling: false`. Release smoke checks minimize and hide-to-tray on macOS, Windows, and Linux; it records platform variance but does not add a second presence authority.

The server updates the existing per-connection foreground registry for every owner `slot_focused` frame. It starts thread X's away episode only when a `hidden: true` null arrives from the last owner connection whose existing row names X. Other nulls, slot replacements, and socket-finally cleanup may clear or replace registry rows for their existing jobs, but they never arm a warm brief.

The gateway stores X's away start and episode generation in memory. The first later owner focus frame ends that episode. It is a return only when it names X; a frame naming Y or null drops X's episode without a call. Return-time gates then require 30 minutes, an open task, a real original request, and a task fingerprint not already claimed. This placement lets a task begin or worker progress land while the user is away.

A normal tab close first fires `visibilitychange` to hidden and sends the hidden-leave while the socket is open. That send remains best-effort. If it is lost, or a crash, power loss, sleep, socket loss, or gateway restart erases the in-memory episode, that return can miss a warm brief. Maintainer `iamwhatever` explicitly accepted those false negatives rather than adding socket-gap inference. The ordinary chat and unchanged cold welcome remain available.

Only the qualifying task fingerprint claim is durable before the visible attempt. Claim-before-preparation prevents duplicate spend across repeated focus, simultaneous tabs, reload, or restart after a return was admitted. Away detection remains in the existing in-memory foreground registry.

### 3.4 Preparation before the model

On an eligible return, the gateway re-reads the durable task snapshot and atomically claims the away episode plus task fingerprint. Only a current, unclaimed snapshot proceeds.

The gateway then posts this deterministic ordinary crewmate message before starting the model call: **“Preparing your current-task summary. You can wait here.”** Producing this message uses no model and no token. It is an `assistant` transcript row tagged `warm_task_brief_preparing`; no new route, listener, status channel, card, widget, or special renderer is added.

The preparing message has one lifecycle:

| Event after preparation | Visible result |
|---|---|
| Model result stays current | A second ordinary crewmate message posts the warm task brief |
| User sends, resets, closes, or replaces the chat | Stale result is discarded; posted messages stay |
| User leaves after the opener is posted | Result still posts as ordinary history |
| Worker/task state changes, a new assistant answer arrives, or the task completes/cancels | Stale result is discarded; posted messages stay |
| Model fails, times out, or returns invalid data | A second ordinary crewmate message says “I couldn't prepare the current-task summary.” |

Preparation, result, and failure rows are ordinary crewmate messages. Once posted they stay as ordinary history and follow the normal transcript save path; nothing retracts or rewrites them. A stale result is never posted, so a stale attempt leaves only the preparing message. The fixed preparing and failure lines and the three section headings are English, like other gateway-authored lines; the model writes the current status and suggested actions in the language the user writes in.

There is deliberately no `MateResumeCard` fallback; maintainer `iamwhatever` accepted silence when the explicit leave signal is lost, and cold welcome remains available under its unchanged rules.

### 3.5 Durable input and output

The model prompt snapshot is built on the gateway from existing durable local state:

1. **Original request:** the first genuine user request for the active task, quoted verbatim when it fits the existing transcript-summary user-text bound, otherwise faithfully quoted with an explicit truncation mark. The model never chooses a different origin.
2. **Current task:** the session ledger's `goal`, `phase`, `next`, recent event tail, and artifacts when present; the work ledger's item title, state, worker status, last truthful worker summary, decision, verdict, and artifacts when present; and bounded recent user/assistant transcript turns for context.
3. **Next steps:** one to three actions inferred from that snapshot.

Missing state stays missing. The prompt forbids invented work, progress, percentages, dates, deadlines, elapsed-time claims, and ETAs. Worker transcript text is not read; only the report the worker deliberately wrote to the shared work ledger is eligible.

The model returns a small structured result. The gateway validates it, supplies the exact source quote itself, caps the current-state facts and suggestions, and applies existing display redaction. The visible second message has exactly three parts: **Problem**, which holds the exact original-request quote; **Current status**; and **Suggested actions**. Suggested actions contain one to three self-contained actions for the user. Empty or malformed output produces the ordinary failure message from section 3.4.

Suggested actions are guidance for the user, not reply choices or hidden commands for the crewmate. If the user wants the crewmate to act, their next message states that action in their own words. The generated brief stays out of model replay even when the reply refers to it: admitting model-written suggestions as new model evidence would let generated claims become task truth and recursively ground later work.

A successful brief is a durable `assistant` transcript row tagged `warm_task_brief_result`, rendered through the ordinary crewmate-message path and attributed to the selected crewmate. Role `assistant` is required by that ordinary path: a distinct role would need the card-like or special result renderer this amendment removes. Preparation, result, failure, and claim rows are excluded from task fingerprints, session summaries, model replay and recall, history consolidation, compaction evidence, auto-nudge evidence, metadata-stripping recent projections, and session transfer, so generated speech cannot become new task truth or feed another model call. Every transcript-to-model or metadata-dropping reader applies the shared `is_warm_task_brief_message` predicate from `warm_task_brief_contract.py` before it removes `meta`; focused tests feed generated rows through replay, recent projection, transfer, consolidation, compaction, and nudge readers and fail if any one admits them. Existing redaction runs before persistence. The durable claim and row fingerprint prevent reload from regenerating or duplicating it.

### 3.6 One bounded model call

Since the section 3.8 amendment this one-call brief is the fallback: a qualifying return first wakes the crewmate, and this call runs only in the cases 3.8 lists.

The warm task brief uses `llm_helpers.run_bg_oneliner` with the governed background model, a bounded prompt, a bounded output, a timeout, and no tools. This is the existing model path: no new provider, endpoint, listener, bind, ingress rule, credential path, or third-party data send is added.

Each completed or failed attempt is charged to the owning session as `warm_task_brief` background work, using the existing input-token, output-token, credit, and duration accounting. The Usage view and crew log therefore show the spend.

The old warm resume used no tokens. The replacement can spend one background model call on one eligible return for each materially changed task snapshot. It spends nothing while visible, while hidden, on a short return, on repeated focus, when another tab remains foreground, for cold welcome, for unchanged state, for terminal work, or for a duplicate claim.

### 3.7 Deduplication, cancellation, and stale results

A canonical task fingerprint covers the original-request identity, the latest genuine user or assistant task row, session-ledger revision, and work-ledger report revision. It contains hashes and sequence values, not copied user text. Preparation, result, and failure rows are explicitly excluded, so publishing a brief cannot make its own task look changed.

Before posting the preparing message or calling the model, the gateway atomically claims the task fingerprint in the transcript and consumes the in-memory away episode. Claim-before-message means repeated focus, two returning tabs, reconnect replay, or gateway restart cannot show two attempts or spend twice on the same task snapshot. A crash after the claim may lose that brief, but cannot loop and spend again while nothing changed.

A later hidden-leave → 30 minutes away → return cycle is eligible only when its task fingerprint differs from the last claimed attempt. No previous claim makes the first eligible return valid. A new user message or worker event can change the fingerprint, but does not itself trigger a call; the next qualifying return does.

The gateway rechecks the foreground episode, slot identity, task fingerprint, and terminal state before preparation and before the model call, and everything but the foreground episode before publication: once the opener is posted, leaving does not drop the result. Any mismatch discards the result; messages already posted stay. Once model acquisition starts, cancellation lets the governed call finish and publication suppression discards any stale output afterward.

### 3.8 Writer amendment, 2026-10-28: the crewmate writes the brief

**Why.** The one-call brief summarizes what the ledger last said. The ledger only changes when a worker or the crewmate writes to it, so the brief can say a pull request is waiting for review after it merged, or is green after it went conflicting. A gateway-side fix that read live pull-request state before the call ([#25307](https://github.com/kirodotdev/KiroCrew/pull/25307)) was closed by the owner in favor of this design: it covered one task kind, and the gateway would have had to learn every other kind (files, deployments, tickets) itself. The crewmate already has the tools for every task it owns. Issue [#25306](https://github.com/kirodotdev/KiroCrew/issues/25306) tracks the stale report.

**Decision.** On a qualifying return the gateway no longer starts with `run_bg_oneliner`. It wakes the crewmate that owns the thread for one turn. In that turn the crewmate:

1. Checks the live state of each open task with its own tools, by what the task's acceptance points at: a pull request's state and checks, a file, a deployment, a ticket. Nothing in the wake is specific to GitHub.
2. Records what it found in its work ledger and its Dashboard fields (allowed writes below).
3. Publishes the brief through one structured publish call carrying the section 5.1 fields (`status`, `headline`, `needs_you`, `body`; since the 2026-10-31 amendment, no `widget`).

The gateway validates, redacts, defangs and caps the published fields exactly as it does for the one-call result. It posts the row tagged `warm_task_brief_result` under the existing claim. A wake that ends without a valid publish call counts as a failed wake (fallback below).

**Trigger.** Unchanged: sections 3.2 and 3.3 as amended on 2026-10-18 and 2026-10-22. The same durable claim, fingerprint dedup, and fixed opener come first. The opener is posted before the wake starts and costs no model call.

| State at return | What runs |
|---|---|
| Qualifying return, crewmate idle, under caps, host not under pressure | Wake turn |
| Crewmate already has a turn running | No wake. The existing deferral waits for that turn; then the one-call brief runs on the ledger that turn left |
| Daily wake cap reached | One-call brief |
| Host resource posture is `critical` | One-call brief |
| Wake fails, times out, or ends without a valid publish | One-call brief |
| Wake setting turned off | One-call brief |

**What the wake may do.** The wake turn reads and reports. It is a background-priority turn: it never preempts a turn the person starts, and a typed row from the person cancels it like any brief in preparation (2026-10-26 leave amendment).

| Allowed | Refused |
|---|---|
| Read tools: files, `gh pr view`, check runs, deploy and ticket status | New dispatch: spawning sub-agents, sending to other sessions, ledger `create` or `bind` |
| Ledger `verdict` with the acceptance evaluator's answer | Ledger `decide` (it instructs workers) |
| Ledger `close` with `accepted`, only after the evaluator returned `pass` in this turn | `close` with `rejected` or `abandoned` |
| Dashboard field writes | Pushes, merges, comments, or any other write to a remote system |
| The one brief publish call | Deletes, resets, or any other destructive action |

The gateway enforces this list for the wake turn, not the prompt. A refused tool fails at once instead of raising an approval card, because the person has just returned and a card on top of the brief is noise. The turn also has a tool-call budget, `WARM_TASK_BRIEF_WAKE_MAX_TOOL_CALLS = 15`; past it every call but the publish is refused.

**Progress stays gateway-counted.** After the wake turn ends, the gateway reads the ledger and computes `progress` (`accepted` of `total` work items for the current goal) itself, so a `close` the wake just wrote counts. A number in the published fields is ignored, as in section 5.1. The crewmate cannot invent progress; it can only move it by a ledger write the evaluator backed.

**Spend.** This is the main cost of the change. A one-call brief is one small tool-less model call. A wake is one full agent turn: the crewmate's startup context plus every tool call and result. One return can cost tens of times what a one-call brief costs. Caps:

- At most one wake per qualifying return (the existing claim already enforces this).
- `WARM_TASK_BRIEF_WAKE_MAX_PER_THREAD_PER_DAY = 6`. Past it the thread uses the one-call brief until the next local day.
- `WARM_TASK_BRIEF_WAKE_MAX_PER_OWNER_PER_DAY = 20`, across every crewmate thread.
- The 15-tool-call budget above.

The existing ceiling of 48 briefs per thread per day still bounds the total. The owner's conductor thread had about four qualifying returns on 2026-10-17, inside the per-thread cap. Each wake is charged to the owning session as `warm_task_brief_wake` background work through the existing token, credit and duration accounting, so the Usage view and crew log show it apart from `warm_task_brief`. A fallback after a failed wake charges both.

**Timeout and fallback.** `WARM_TASK_BRIEF_WAKE_TIMEOUT_SECS = 180`. At the deadline the gateway cancels the wake turn and runs the one-call brief with its existing `WARM_TASK_BRIEF_TIMEOUT_SECS = 60`. Ledger and Dashboard writes the wake made before the deadline stay: each was a checked fact. The fallback reads the snapshot again, so it sees them. The same fallback runs when the wake cannot start, errors, ends without a valid publish, or the host posture is `critical` at return. The person never returns to the opener alone, short of the one-call fallback failing too, which posts the existing failure line.

**Latency and UI.** A wake can take tens of seconds, and the worst case is about four minutes (180 s wake plus 60 s fallback). The UI change stays small. The existing opener is the placeholder: it already says the crewmate is catching the person up. The brief card lands below it when ready, through the ordinary row path. The wake turn's own steps draw collapsed, like any background turn's, so the chat does not fill with tool calls. No spinner, progress bar or new status channel is added.

**Records.** The wake prompt row, the turn's steps, and the published brief carry the warm-brief tag, so the section 3.5 reader filters keep generated speech out of replay, fingerprints, summaries and recall. The ledger and Dashboard writes are ordinary records: they change the fingerprint like any other write, which is correct, since they are new facts.

**Setting and undo.** One setting, `dashboard.warm_brief_wake`, default on. Off restores the one-call brief with no migration, because the published row has the same tag and shape. The two-release rollback in section 9 is unchanged for removing the brief as a whole.

**Validation (focused tests only).**

- A qualifying return with an idle crewmate posts the opener, then starts exactly one wake turn and no `run_bg_oneliner` call.
- A return while a turn runs starts no wake; the one-call brief runs after that turn ends.
- Each fallback cause (cap, `critical` posture, error, 180 s timeout, no publish, setting off) runs the one-call brief exactly once and posts exactly one card.
- Every refused tool in the table fails without an approval prompt; an allowed read runs; `close accepted` without an in-turn `pass` is refused.
- A published `progress` number is ignored; the posted progress matches the ledger after the wake's writes.
- A typed row cancels the wake and posts no stale card.
- Wake spend is recorded under `warm_task_brief_wake`.

**Open questions.**

1. **Running turn at return.** This design reads "that turn reports instead" as: no wake, and the one-call brief runs after that turn ends on the ledger it left. The other reading is no brief at all, with the running turn's answer as the report. The owner should confirm.
2. **`close accepted` in a wake.** Letting the wake close an item it verified keeps progress true when the person returns. Leaving it out keeps the wake strictly reporting, but the card can then show 1 of 3 while the bar is met. This design allows it, behind the evaluator's `pass`.
3. **Context size.** A wake could load a trimmed context (no memory or lesson replay) to cut spend. That may also cut the judgment the brief needs. This design keeps the crewmate's normal context and relies on the caps.
4. **Cap values.** 6 per thread and 20 per owner per day are first guesses from one owner's use. They should be read again after a week of `warm_task_brief_wake` spend.

**Risks.**

- **Spend.** A crewmate with many threads and frequent returns can reach the owner cap daily. The cap turns the overflow into one-call briefs, not silence.
- **A read that writes.** Some "read" commands have side effects (a CLI that refreshes a cache or a login). The refused list stops remote writes; local side effects of allowed reads are the same as in any normal turn.
- **Wrong verdict.** A wake could close an item on a misread. The evaluator `pass` gate bounds this to the evaluator's own error rate, the same bar a normal turn uses.
- **Slower brief.** Most returns now wait longer for the card. The opener keeps that wait explained.

### 3.9 Redesign amendment, 2026-10-31: verify, then ask

**Why.** The owner reported that the brief does not let them act: too much data, too little that is true and usable. A study of the owner's real briefs backs this up. Code read on `main` at `29785b6c6`; `wtb` below is `src/kiro_crew/dashboard/warm_task_brief.py`.

| Finding | Count |
|---|---|
| Real briefs found (all in one conductor crewmate thread, 2026-10-21 to 2026-10-30) | 20 results, 2 openers with no result |
| Briefs stating at least one stale or false fact (a merged PR "to merge", an RFC "to approve" that had shipped, "8 PRs merged" when none had) | 17 of 20 |
| Owner's next message asked for the real status, the missing options, or corrected the brief | 17 of 22 |
| Briefs carrying any option from the `[OPTIONS: ...]` choices pending just before the owner left | 0 of 19 |
| Briefs giving a reason and a recommendation for each ask | 0 |
| Briefs mostly in English while the owner writes Chinese | 17 of 20 |
| Briefs showing a whole-board count such as "142 of 170 done" | 12 |
| Crew Mode root briefs | 0 (the Crew Mode path merged 2026-10-30; no data yet) |

The causes, in order of weight:

1. **The model never sees the chat.** `_build_prompt` serializes the snapshot with `transcript` as the last key and cuts the JSON at 40,000 characters (`wtb:936-947`). The snapshot holds every work item on the board, terminal ones too, in file-name (random id) order. On the studied thread the items alone were 63k to 85k characters, so the transcript was always cut away and the model saw a random slice of old items, cut mid-JSON. This is why briefs on 10-30 asked the owner to merge PRs finished on 10-10, and why they were in English: the language rule reads the transcript (`wtb:231`).
2. **Even a visible transcript loses the asks.** Assistant rows keep only 400 head and 400 tail characters (`session_summary.extract_turns`), and the bounded render keeps the first three user turns, so an old goal returns and the table of asks in the middle of the conductor's last message is dropped.
3. **No fact carries a read time.** Worker summaries and statuses are from when they were written; nothing says when each was true, and nothing checks them on return. The section 3.8 wake was accepted to fix this but is not on `main` (no `dashboard.warm_brief_wake` setting exists at `29785b6c6`).
4. **Nothing carries the pending choices.** The prompt asks for a catch-up, not decisions; `needs_you` allows three items of 120 characters, links are forbidden, and `_defang_markers` turns `[OPTIONS` into `(OPTIONS` (`wtb:1010`). Worse, the follow-up chip bar reads the newest assistant row (`website/src/app-sdk/protocol/options.ts`), so the brief, posted below the conductor's question, hides the buttons the owner left behind.
5. **The conductor gets the crewmate prompt.** The conductor role hint is chosen by `slot.mode != DM_SLOT_MODE` (`wtb:1294`), so a conductor crewmate's own thread, which owns the work ledger, never gets it.
6. **A count that misleads.** With `goal_version == 0` progress counts every item on the board (`_goal_progress`), so the number never tracks today's work.
7. **An orphan opener.** The fixed opener posts before the model call (`wtb:1277-1283`); when the result is dropped as stale, the opener stays alone (2 of 22).

The trigger was not a cause: in this data no brief fired on a reconnect or a short pause.

**Decision.** The owner decided seven points. They are binding and written here as the design.

1. **Verify, do not trust.** The brief knows which facts can change while the owner is away and checks their current state before stating them. Every stated fact carries its source and read time. A fact that could not be checked is shown as "not checked since HH:MM", never asserted.
2. **Asks must be usable.** The owner can act from the message alone. Each ask says what, why only the owner can do it, every option with its consequence, which option is recommended and why, and a link. Pending choices render as real, working option buttons.
3. **Quiet return.** When nothing changed and nothing waits on the owner, the brief is one line.
4. **One message.** No separate fixed opener that can be left alone.
5. **Trigger stays on return.** The section 3.2 rule as amended (explicit leave, 30 minutes away, explicit return) is unchanged. The brief is not prepared at leave time: frequent tab switches would fire it too often, and a short absence needs no reminder. While it is prepared, the owner sees that a catch-up is coming, and the final message replaces that indicator in place.
6. **Only useful data.** A number appears only when it is scoped to the current goal and helps a decision.
7. **Scope.** Crewmate (Mate) threads and Crew Mode roots only, as `is_brief_thread` already says (`wtb:381`). "Ultra Mode" is the Settings label of the Crew Mode feature preview (`developer.ultra-mode`, label key `crew_mode`); it is the same mode, not a second one, so it needs no separate rule. Plain chats and worker chats never get a brief.

#### 3.9.1 Volatile facts and how each is checked

A fact is **volatile** when it can change without a row in this thread. The gateway tags each input fact with `source` and `read_at`. A volatile fact is stated as current only when its `read_at` is after the away anchor (section 3.2, as amended on 2026-10-22). Otherwise the brief shows its last value as "not checked since HH:MM", with HH:MM its `read_at` in the owner's local time.

| Fact | Volatile | Checked by | When it cannot be checked |
|---|---|---|---|
| Work item state, status, reason, decision, verdict | yes | gateway, reads the ledger at assembly | n/a (local read) |
| Worker session running, idle, closed, waiting on an approval | yes | gateway, reads the session registry at assembly | n/a (local read) |
| Pull request state, checks, review, mergeability | yes | the section 3.8 wake turn, with the crewmate's own tools | "not checked since" its last ledger or report time |
| Deployment, ticket, any other remote state named by an acceptance | yes | the section 3.8 wake turn | same |
| Goal, original request, the conductor's last message | no | gateway, from the transcript and ledger | n/a |

The gateway does not read GitHub itself: the owner closed that approach in favor of the wake (section 3.8, #25307). Until the wake ships, and on every one-call fallback, remote facts are shown with their last read time. The prompt names the `read_at` fields and forbids stating a volatile fact without one.

#### 3.9.2 Message shape

One assistant row, tagged `warm_task_brief_result` as today, in the owner's language. Each section is left out when empty, never padded.

| Section | Holds | Limit |
|---|---|---|
| Goal | What this thread is for now | 1 line |
| Since you left | Only changes after the away anchor (merged, newly red, worker done or blocked, new question), each with source and read time | 4 lines |
| Needs you | The asks, each in the 3.9.3 form | 4 asks |
| Problems | Stuck things that do not need the owner, with the cause | 3 lines |
| Next | What runs without the owner | 2 lines |
| Buttons | The pending choices, as working buttons (3.9.3) | as in the source |

The model returns these as structured fields; the gateway caps, redacts and defangs each, as section 5.1 already requires for model text. The fixed card of section 5.1 (the orb of #26500 when it lands) draws them. Progress in the card follows 3.9.5.

**Quiet return.** A brief runs only when the fingerprint changed (patrol and report rows change it). When it runs, no stated fact changed after the away anchor, and no ask or waiting item is open, the brief is one line, "Nothing new since HH:MM. Nothing waits on you.", in the owner's language. When the gateway can decide this from local facts alone (no open ask, no waiting item, no item or worker changed, no remote fact pending a check), it posts the line itself and makes no model call.

#### 3.9.3 Asks

The pending choices come from parsed data, not from model text. The **source message** is exactly the row whose choices the chip bar shows at return time: the gateway applies the same rules as `deriveFollowUpOptions` (`website/src/app-sdk/protocol/options.ts`) to the thread's history, with one shared fixture set pinning both. So the source can be an assistant row or a note row with options; a patrol or wake turn that posted newer choices while the owner was away is the source; and a Stop card, a queued row, or a newer drawn row with no marker means there is no source, and no marker is copied. There is no away-anchor bound. Open work items that wait on the owner (the section 5.1 `waiting` list) are asks too.

Each ask is a structured field: `what`, `why_owner` (approve, merge, decide, reply to a named person), `options` (each `label` and `consequence`), `recommended` with `why`, and `link`.

- **Labels are copied, never written.** The option labels must equal the source marker's labels, in order. The model may add a consequence and mark one recommended; it may not add, drop or rename a label. On a mismatch the gateway keeps the source labels and drops the model's consequences and recommendation for that ask.
- **Recommendation is the source's.** When the source message recommended an option, the brief repeats that recommendation unless a checked fact contradicts it, and then says which fact.
- **Working buttons.** The gateway appends the source marker, verbatim, as the last line of the brief row's `content`. The existing chip bar reads the newest assistant row, so the buttons the owner left now appear under the brief and work as they did before. Model text keeps `_defang_markers`; only the gateway-copied marker is live. This narrows section 5.1's defang rule; it opens no new input path, since the marker was already shown as buttons in the same thread.
- **Resolved asks lose their buttons.** When a checked fact shows the source ask is already done (the PR merged, the item closed), the ask moves to Since you left and the marker is not appended.
- **Links.** `link` is drawn as a real link only when the URL appears verbatim in the input or the gateway builds it from an acceptance's `repo` and `pr`. Any other URL is dropped. This supersedes the "no links" rule of section 5.1 for this field only.
- **Someone else's work.** An ask about another person's PR names whose it is and what the owner is asked to do with it.

#### 3.9.4 Input assembly

1. Budget each section on its own (source ask, changes since the away anchor, transcript slice, open items) and drop whole entries when a section is over budget. The serialized JSON is never sliced.
2. Order the payload: source ask, changes, transcript slice, open items.
3. Open items only: open work items and items that changed after the away anchor. Terminal items before the anchor are left out. Order by last activity, newest first.
4. The transcript slice is the rows after the owner's last genuine message, whole, newest first under its budget; no 400+400 excerpts.
5. Reply language is that of the owner's last three genuine turns. The indicator label, quiet line and failure line use the same language.
6. The conductor prompt is used whenever the thread owns a work ledger, whatever the slot mode.

#### 3.9.5 Numbers

A count is shown only when it is scoped to the current goal: `progress` is drawn only when `goal_version > 0`, so a board that never recorded a `new_goal` shows no count (the orb's "No count yet"). Whole-history counts are never shown. Any other number must come from an input field scoped to the current goal and must bear on an ask.

Note, 2026-10-31 (owner decision, the redesign's answer 6: show only useful data): a brief with no count draws no orb at all, not an empty ring captioned "No count yet". The waiting cards, their "N more" toggle and the status tag still show. This replaces the empty ring and its caption in the orb amendment of 2026-10-31 (#26500) and in the "No count yet" wording above; a brief with a count draws the orb as before.

#### 3.9.6 Output check

Before posting, the gateway checks the model's fields. Every `#N` reference and every commit SHA must appear in the input; every HH:MM must equal a `read_at` in the input; every volatile fact stated as current must cite a field read after the away anchor. On any failure the model's fields are dropped and the gateway posts what it built itself: the asks from the source marker and the `waiting` list, their buttons, and one line saying the summary could not be written. The existing invalid-output failure line still covers an empty or malformed result.

#### 3.9.7 One message and the indicator

No opener row is written. On a qualifying return, after the durable claim, the gateway raises the slot's existing display-only `preparing` flag (`dashboard/slot_preparing.py`) with a kind, `catch_up`, so the client draws "Preparing your catch-up" at the end of the thread where the brief will land. The brief row is posted inside that span, as the flag's contract already requires, so the row replaces the indicator in place.

| Event | What the owner sees |
|---|---|
| Brief ready | The indicator turns into the brief |
| Owner types, resets, closes or replaces the chat | The indicator drops; nothing is posted |
| Owner leaves during preparation | Preparation continues; the brief lands as ordinary history |
| Model fails, times out or returns invalid data | The indicator turns into the failure line |
| Gateway restarts during preparation | The indicator and the work go together; the claim stops a second spend |

The 2026-10-26 leave rule now reads from the claim, not the opener: once claimed, leaving does not cancel. A stale attempt leaves no row, so no message is ever orphaned.

#### 3.9.8 What does not change

The return trigger and its rules (sections 3.2 and 3.3 as amended), the 30-minute minimum, the durable claim, fingerprint dedup (an unchanged fingerprint still posts nothing), deferral behind a running turn, the 48-per-thread daily ceiling, the section 3.8 wake and its caps and allowlist, the reader filters of section 3.5, privacy and authorization (sections 6 and 7), and the cold-welcome non-goals. No new route, listener, credential or remote read is added: the only remote reads are the wake's, already accepted.

**Supersedes.** The fixed opener and two-message shape (2026-10-15 and 2026-10-16 amendments, section 3.4's preparing message, section 4's opener step); "nothing retracts" for the opener, since none is written; the whole-board progress fallback of section 5.1; the "no link" and full defang rules of section 5.1 for the gateway-built `link` and the gateway-copied marker only; the language-from-original-request rule; the slot-mode choice of the conductor prompt; and the report idea of preparing the brief at leave time, which the owner rejected.

**Alternatives considered.**

- **Prepare at leave time.** Rejected by the owner: tab switches would fire it often, and short absences need no reminder.
- **Gateway reads GitHub on return.** Rejected earlier by the owner (#25307) in favor of the wake, which covers every task kind.
- **Let the model write the `[OPTIONS: ...]` line.** Rejected: model text could invent or rename a choice that becomes a one-click send. Copying the source marker keeps the buttons exactly what the owner was offered.
- **Keep the opener and edit it into the brief.** Rejected: rewriting a posted row changes history that readers and exports already hold. The display-only flag needs no rewrite.

**Validation (focused tests only).**

- A snapshot larger than the old 40,000-character cap still carries the source ask and transcript slice; no JSON is sliced; terminal items before the anchor are absent.
- A volatile fact read before the anchor renders as "not checked since HH:MM"; one read after it renders as current with its source.
- The brief row's `content` ends with the source marker verbatim and the chip bar shows its buttons; a model-written marker stays defanged; a resolved ask posts no marker.
- The gateway's source pick equals `deriveFollowUpOptions` on shared fixtures: a patrol row or note row with newer choices posted while away is the source; a later row without a marker, a Stop card or a queued row means no marker is copied; a failed turn is crossed as the chip bar crosses it.
- Option labels the model renamed are replaced by the source labels.
- A `#N` or SHA absent from the input makes the gateway post only its own parts.
- A quiet return posts one line and, when decided locally, makes no model call.
- No opener row is written; the `catch_up` indicator shows during preparation and drops on success, stale, cancel and failure.
- A board with `goal_version == 0` shows no count.
- The language follows the owner's recent turns, not the original request.
- A DM conductor thread that owns a work ledger gets the conductor prompt.
- A plain chat and a worker chat never get a brief.

**Rollout.** This amendment merges first. One implementation PR follows, linking this section. The section 9 undo still applies.

## 4. Timing

```mermaid
sequenceDiagram
    participant U as User tab
    participant G as Gateway
    participant D as Durable claim
    participant M as Model

    U->>G: slot_focused(null, hidden=true) for X
    G->>G: record X away start in memory
    Note over G,M: No timer and no model call
    U->>G: first later slot_focused(X)
    G->>D: check away >= 30m and task changed
    G->>D: claim episode + fingerprint
    G-->>U: show the catch_up preparing indicator (3.9.7; no opener row)
    G->>M: wake turn with the 3.8 tool allowlist, or one tool-free fallback call
    alt snapshot still current
        M-->>G: published or structured task brief
        G-->>U: post the one brief row in place of the indicator
    else user sends, resets, closes, or the task changes
        G-->>U: drop the indicator, post nothing
        M-->>G: stale result discarded
    end
```

## 5. User-visible shape

Before this change, opening an active-task chat shows the immediate fixed warm resume.

| Scenario | Result after change |
|---|---|
| Open active-task chat | No immediate warm resume |
| Stay on page for 30 minutes | No new warm welcome |
| Leave window unfocused but page visible | No away episode and no warm welcome |
| Stay away for 30 minutes | No work while away |
| Return after 30 minutes, changed task | Preparing message, then one grounded crewmate message |
| Return after 30 minutes, unchanged task | Nothing |
| Tab/browser close, crash, or sleep sent no leave update | No warm brief; accepted false negative |
| Gateway restarts during away period | No warm brief; accepted false negative |
| Candidate changes after preparation | Preparing message stays; stale result never appears |
| Open cold chat | Unchanged cold welcome |

### 5.1 UX amendment, 2026-10-22: structured brief card

The owner found the one-paragraph prose result tiring to read, with no focus. The result is now structured fields drawn by a fixed card. This supersedes the "not a card, widget, ... or special result row" clause of the 2026-10-15 amendment, the one-prose-message shape of the 2026-10-16 amendment, and the "no card or special renderer" wording of section 3.5. Trigger, preparation, spend, staleness and reader-filter rules are unchanged.

- The model returns JSON with four plain-text fields: `asked`, `now`, `needs_you[]` and `next[]`. Each is redacted, has chat control markers (`[OPTIONS:`, `[STEERING`, `<!--`) defanged so no reader of `content` can turn a field into send buttons or chips, and is length-capped by the gateway. `meta.brief` holds the fields. `content` holds the same brief as labelled plain text, for export and any reader without the card.
- The card is the body of the ordinary assistant bubble (`AssistantMessage`'s `body` prop), drawn in place of the markdown. It shows every field as a plain text node: never markdown, HTML, a link or a button.
- Only an `assistant` row draws the card. Any other role carrying `meta.kind: warm_task_brief_result` draws as an ordinary row of its own role, so a user row can never pass as the crewmate's brief.
- The row keeps the timestamp and row actions of any assistant reply: Copy (the plain-text `content`), Quote and Reply in thread. The raw-markdown toggle is hidden on brief rows, since no markdown is on screen.
- A brief row without a valid `meta.brief` (an older prose brief) keeps the ordinary markdown bubble.

**Amendment, 2026-10-24: a manager's report.** The four fields above are replaced. The prompt casts the model as a professional manager reporting to the owner: lead with whether it is on track and what needs the owner, remind them what they asked, say how far from the goal in real terms, no filler, no retelling the transcript, never an invented number. The redaction, defang, cap and plain-text rules above apply to every new field.

| Field | Source | Limit |
|---|---|---|
| `status` | model; `on_track`, `needs_you`, `blocked` or `done`. Anything else reads as `needs_you` when `needs_you` has items, else `on_track`; `on_track` or `done` beside `needs_you` items reads as `needs_you` | one word |
| `headline` | model; required. An older `now` or prose `message` reads as it | 240 chars |
| `asked` | model; falls back to the gateway-quoted original request | 160 chars |
| `progress` | **gateway**: `{accepted, total}` over the current goal's work items (after a `new_goal` write, only the items it created); left out when there are none or the goal's items cannot be told apart. Model numbers are ignored | — |
| `remaining` | model; what is left before the goal is met | 160 chars |
| `done` | model | 4 items |
| `needs_you` | model; each the action plus what it unblocks | 3 items |
| `next` | model | 3 items |
| `extra` | model; `{title, items}` sections for a risk, a cost or a choice; empty ones dropped | 2 sections, 3 items each |

**Amendment, 2026-10-26: free form.** The prompt now asks only for `status`, `headline`, `needs_you`, a free-form plain-text `body` (lines kept, 12 lines, 900 chars; when present, `asked` no longer falls back to the quoted request) and an optional `widget`: the HTML body of one widget, redacted and defanged, dropped whole over 6,000 chars, drawn below the text by the same sandboxed `WidgetFrame` a chat `<mcwidget>` uses, with no added grants. The prompt tells it to use theme variables, fit 320 px, animate only under `prefers-reduced-motion: no-preference`, and draw no buttons, links or numbers the snapshot lacks. `progress` stays the gateway's. `content` stays plain text and never holds the widget. The older fields still parse and draw.

**Amendment, 2026-10-31: a fixed progress orb.** The prompt asks for words only (`status`, `headline`, `needs_you`, `body`); a `widget` the model returns is ignored. The gateway adds `waiting`: every open work item whose status is `blocked` or `question` with reason `approval` or `needs_human` (the ledger's `WORK_REASON_STATUSES` and `WORK_BLOCKED_REASONS`) that the conductor has not already answered with `decide`, as `{title, ask}` from its title and summary, cleaned like a model field, in ledger order, at most 50, with `waiting_total` counting them all. The model is told not to repeat those in `needs_you`, and a `done` or `on_track` status beside anything waiting reads as `needs_you`. The card replaces the highlighted Needs you box and the progress bar with one fixed picture: an orb whose fill and glow are `accepted / total`, sparkles when full, an empty ring captioned "No count yet" when there is no count, and above it the waiting items then the model's `needs_you` as plain-text lines with a warn edge (not styled as controls). The first three show; the rest fold behind one "N more" toggle that also counts items past the gateway's list. Motion is finite (the fill rises once, a full orb twinkles three times) and only under `prefers-reduced-motion: no-preference`; colours are theme variables; the orb is a `progressbar` labelled with the count. `content` lists every waiting item. A stored brief with a `widget` still draws it last in `WidgetFrame`, and an older `now` brief with no status and no count keeps its plain Needs you list.

The card draws them in that reading order: status chip and headline, Needs you (highlighted), Progress (a bar only when `progress` exists, plus `remaining`), What you asked, Done, Next, then the `extra` sections. `content` carries the same order as labelled plain text. An older `meta.brief` (`asked`, `now`, `needs_you`, `next`) still draws, `now` as the headline, with no status chip.

## 6. Spend and privacy

Prompt input can include the user's original request, bounded transcript excerpts, session-ledger state, and shared work-ledger reports. All input stays within the model provider and account already configured for Kiro Crew. No project code, transcript, ledger, credential, or user data is sent to a new service. Existing transcript privacy modes still win; an incognito or otherwise derivation-withheld transcript is ineligible.

The section 3.8 wake path is not tool-free. The wake is a crewmate turn that calls the crewmate's own tools with the crewmate's own credentials and approval scope, so its read tools (`gh pr view`, check runs, deploy and ticket status) reach the same remote systems a normal turn of that crewmate reaches. The wake adds no new service, credential, listener or route. It is narrower than a normal turn: the gateway refuses every tool outside the 3.8 allowlist, including pushes, comments and other remote writes, and caps the turn at `WARM_TASK_BRIEF_WAKE_MAX_TOOL_CALLS`. Only the one-call fallback of section 3.6 stays tool-free.

The implementation PR body must repeat this spend statement, name the 30-minute away threshold, state that return is the only trigger, and state that the deterministic preparing message costs no model call.

## 7. Authorization and exposure

This change adds no route. It extends an existing owner-only WebSocket presence frame and its server registry. Existing WebSocket origin checks, dashboard authentication, owner/audience checks, app-token denial, transcript privacy checks, provider authorization, and model governance remain unchanged. No listener, public bind, port publication, ingress rule, storage ACL, or auth bypass is added or weakened.

The source fingerprint and durable claim are gateway-owned derived state. They contain no raw request or ledger text, use owner-only storage, follow transcript purge/reset rules, and are never served as an authorization input. The away start stays in memory and is never written.

## 8. Validation

The implementation uses focused tests only.

- A before/after test proves an active-task chat emits no immediate warm resume.
- Existing cold-welcome tests remain unchanged and pass, proving every cold path and visible behavior stayed intact.
- **Visible for 30 minutes or longer:** no message and no model call.
- **Visible but window-unfocused for 30 minutes or longer:** no away episode, message, or model call.
- **Hidden for 30 minutes or longer before return:** no message and no model call.
- **Return before 30 minutes:** no message and no model call.
- **Duplicate focus or multiple tabs:** one return edge, one preparing message, and one model call.
- A return-boundary test proves the deterministic preparing message is persisted and visible before `run_bg_oneliner` starts and costs no model call itself.
- A result test proves **Problem** contains the exact original request, **Current status** stays truthful, and **Suggested actions** contains one to three user-owned actions.
- A state test runs two qualifying away/return cycles with no external task change and proves one model call: generated task-brief messages never alter the fingerprint.
- Race tests prove a user send, a leave before the opener, worker/task change, newer assistant answer, completion, or cancellation suppresses the stale result.
- Message tests pin ordinary crewmate rendering and the exact failure text.
- Lifecycle tests prove reload, reset, close, slot replacement, timeout, model failure, and invalid output post no stale result; a gateway restart during away loses the in-memory episode and makes no call.
- Reconnect tests prove a hidden first connect/reconnect sends no focus frame and cannot return; the later visible transition sends the only return frame.
- Presence tests prove chat routes report `chat.activeSlot`; the Crewmates page reports a separate visible mounted thread regardless of focus; focus-aware `viewedThread` remains unread-only; only `slot_focused(null, hidden=true)` from the last connection arms away; app sockets cannot arm it.
- Negative presence tests prove chat/member switches, non-chat routes, an open New crewmate card, split-view placeholder focus, and socket cleanup create no episode or call; hiding on X then returning on Y consumes X's episode, so opening X later makes no call.
- Electron tests pin that the dashboard view does not set `backgroundThrottling: false`; release smoke checks minimize and hide-to-tray on macOS, Windows, and Linux, while occluded-but-visible and unfocused windows make no synthetic signal.
- Privacy and security tests prove derivation-withheld transcripts are skipped, the one-call fallback is tool-free, the wake turn can run only the section 3.8 allowlist under the crewmate's own credentials, existing auth is unchanged, and no new listener, route or credential path is added.
- Run targeted backend and frontend tests plus lint and type checks for touched packages; do not run the full pytest suite.

## 9. Rollout and undo

1. Merge this RFC first, with `status: accepted`, so the implementation reads the decision from its base.
2. Land one implementation PR for the warm branch, trusted return detection, an in-memory away episode, a durable return claim, two ordinary crewmate messages, focused tests, system docs, locale cleanup, and spend statement.
3. Do not use a First Principles override. Do not merge the implementation until PR Readiness is green.

Undo is a two-release rollback because ordinary `assistant` rows outlive feature code. First disable new generation while retaining `warm_task_brief_contract.py` and every reader filter, then rewrite every tagged preparation, result, and failure row to an empty display-only `notice` claim under its existing row id. Only after that migration is durable may a second release remove the filters, return detection, and model call and restore the immediate warm resume. The cold branch never changes; in-memory away state disappears with the process.

## 10. Non-goals

- Changing any cold-welcome trigger, read, copy, component, placement, failure state, repeat rule, or user-visible result.
- Changing Mate's first welcome, dashboard-created crewmate greetings, or the greet route.
- Changing custom-agent `welcomeMessage` behavior or KAS projection.
- Calling an LLM because 30 minutes passed.
- Running a periodic 30-minute timer.
- Calling an LLM while the page is visible, while it is hidden, or on a return under 30 minutes.
- Adding a new model provider, external service, route, listener, auth path, or data destination.
- Inventing progress, percentages, deadlines, or ETAs.

## 11. Alternatives considered

- **Thirty-minute idle timer.** Rejected: idle is not return. It would spend while the user stayed on the page or was still away.
- **Client-supplied away timestamp.** Rejected: tabs and clocks disagree, and the gateway already owns authenticated per-connection foreground state.
- **Treat socket disconnect as leave.** Rejected: a network outage is not proof the user left.
- **Use `slot_read` as return.** Rejected: it is a throttled, best-effort unread relay and stores no presence.
- **Delay the fixed warm card.** Rejected: it still lacks the original request and cannot explain the task in current terms.
- **Start the model before the preparing message.** Rejected: the user would see an unexplained wait, and a message that appears later would lie about when work began.
- **Retry a claimed return.** Rejected: automatic retry can repeatedly spend while nothing changes. A changed task and a new qualifying away/return episode are the retry boundary.

## 12. Decision record

Accepted by maintainer `iamwhatever` on 2026-10-14 in [the maintainer decision comment](https://github.com/kirodotdev/KiroCrew/pull/20157#issuecomment-6167061835), with these terms: warm only; cold unchanged; leave, at least 30 minutes away, then return as the sole trigger; no visible-idle, hidden, short-return, periodic, socket-gap, or reconnect inference; silence is accepted when sleep, restart, or network loss loses the explicit leave signal; deterministic visible status before the model call; stale/cancelled results suppressed with status cleared; original request, current state, and one to three next steps in the warm welcome; durable dedup and no-repeat spend rules retained.

Amended by the user's 2026-10-15 UX decision: preparation and result are two ordinary crewmate messages; the result sections are **Problem**, **Current status**, and **Suggested actions**; failure is an ordinary second message; posted messages stay as ordinary history and a stale result is dropped unposted; and the reader filters and two-release rollback above keep generated speech from becoming model evidence.

Amended by the owner's 2026-10-22 UX decision (section 5.1): the result is structured plain-text fields drawn by a fixed card as the body of the assistant bubble, on assistant rows only, with the timestamp, Copy, Quote and Reply in thread kept and the raw-markdown toggle hidden.

Amended by the owner's 2026-10-31 decision (section 5.1): the model writes words only; progress and the items waiting on the owner are gateway data drawn by a fixed orb component, replacing the model-drawn widget, the highlighted Needs you box and the progress bar.

Amended by the owner's 2026-10-28 decision (section 3.8): a qualifying return wakes the crewmate for one read-only, background-priority turn that checks live task state with its own tools, updates its records and Dashboard, and writes the brief; progress stays gateway-counted; the one-call brief is the fallback on a running turn, a cap, host pressure, failure or a 180-second timeout.

Amended by the owner's 2026-10-31 redesign decision (section 3.9): volatile facts are stated only after a check after the away anchor, else as "not checked since HH:MM"; asks carry what, why the owner, every option with its consequence, a recommendation and a link, and the pending choices come back as working buttons copied from the source message; one message replaces a display-only preparing indicator in place, with no opener row; a quiet return is one line; only goal-scoped counts are shown; the trigger and scope (crewmate threads and Crew Mode roots, which Settings labels "Ultra Mode") are unchanged.
