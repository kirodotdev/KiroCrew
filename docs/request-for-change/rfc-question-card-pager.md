---
title: Question Card Pager — one question on screen, and answers that carry their question
status: accepted
author: tilakputta
created: 2026-09-24
last-audited: 2026-10-06
audited-at: 9348a25a34
doc-pr:
implementation-prs: [13196, 15250]
tracking-issues: []
supersedes: []
superseded-by: []
---
# RFC: Question Card Pager — one question on screen, and answers that carry their question

- Status: accepted by buluoray
- Author: tilakputta
- Created: 2026-09-24
- Related: `website/src/components/QuestionCard.tsx` (the card this reshapes),
  `website/src/components/PendingQuestionCard.tsx` (the submit path whose text format
  changes), `website/docs/i18n-catalog.md` (the authoring rule the six new keys follow),
  `src/kiro_crew/mcp_tools/control.py` (the `ask_question` tool §3.6 makes blocking)

## 1. Problem statement

`ask_question` can raise several questions in one card, and the multi-question case is
broken in three separate ways. Verified on main `61d5bb577`, re-read at `e88e80778`; the
cited symbols are unchanged between them.

**The answer loses its question.** `asText` in `PendingQuestionCard.tsx` is
`Object.values(answers).join('\n')`, and the `onSubmit` handler hands that string to
`onDirectSend` as the answer. Three questions therefore reach the agent as three bare
lines in map-iteration order, with no indication of which question each belongs to. A
multi-select answer is itself a joined string, so two selections on one question are
indistinguishable from two separate questions answered. The agent is left guessing, and a
stateless card commits that same string as the user's own chat message, so the transcript
is wrong too.

**Nothing distinguishes a multi-select from a single-select.** Both modes render the same
option rows through the same `toggleOption` handler with the same `aria-pressed` toggle. A
user answering a multi-select has no way to know more than one answer is accepted, and no
way to know the card is waiting rather than finished.

**A multi-select strands the user.** Single-select auto-advances on answer. Multi-select
deliberately does not, because one click does not finish it — so on a card whose Submit
is disabled until every question is answered, the only enabled primary control is absent
and the only way forward is the small corner chevron. Users stop there.

The first defect is the one that matters most, and it is small. The second and third are
small too. But fixing them exposes a fourth problem that is not small, and is the reason
this document exists: **the stacked card does not have room for the cues the fixes need.**

The card is an accordion. It carries three separate mechanisms to stay usable — a
per-question `collapsed` map seeded by `initialCollapsed`, a `toggleCollapsed` handler, and
a `max-h-[min(60vh,32rem)]` cap on the card with an inner `overflow-y-auto` scroll region —
plus a `Collapse all` / `Expand all` control driven by `allCollapsed` to manage the result.
Each of those exists to compensate for the same thing: N questions do not fit, so the card
must hide most of them and then offer controls for un-hiding. Adding a mode cue and a
forward affordance per question makes each row taller, which makes the compensation worse.

### 1.1 Before and after

![Two columns, before on main and after this change, both dark mode on a live dev instance. Row one, the card is up: on main the agent has already replied "I've posted the card" and its turn is over, against the ask_question tool row still reading Running while the card waits. Row two, submitted: on main the answers land as a Q. slash A. message from the user and a second turn reads them, against a "3 questions answered" card under the tool row and the same turn continuing](assets/question-card-before-after.png)

The "before" column is `main` at `89b9e7668`, which carries the merged pager from #13196. The "after" column is
the implementation branch for §3.6. Same prompt, same answers.

On main the card is the pager this document accepted, but the tool still returns at once: the turn
ends while the card is up, and the answers come back as a user message that starts a new turn. §1.2
is that defect, and §3.6 is the fix. The other endings (dismiss, a composer reply, a reload) are in
the §3.6 figure.

### 1.2 The answer arrives as the user's own message

Labelling the answers (§3.3) fixed what the agent reads, but not where it reads it from. On
main after #13196, the `ask_question` MCP tool returns at once with "END YOUR TURN", and the
card's answer comes back later as the user's next chat message. Three things follow:

- **Agent-authored text speaks with the user's voice.** The `Q.` half of each pair is the
  question the agent wrote, and it arrives in a user-role turn. Whatever the agent put in a
  question, a later turn reads it as something the user said.
- **The answer is not tied to the call that asked for it.** The agent must end its turn and
  then recognise, in free chat text, which reply answers which card.
- **Every other ending looks like silence.** A dismissed card, a card the user ignored and
  typed past, and a card that expired all leave the agent with nothing to read, so it
  cannot tell "no" from "not yet".

## 2. Design principles

- **One question on screen is bounded by construction.** The accordion's three mechanisms
  all approximate a bound the pager gets for free.
- **One gesture per question on the common path.** A single-select answer should not also
  cost a Next.
- **The answer the agent receives must be unambiguous** without the agent knowing the
  order the questions were asked in.
- **A card is one atomic ask.** Partial submission is not a feature: the answer map is
  keyed by question text, and a map missing entries cannot be told apart from a map that
  was never asked for them.
- **An answer is the result of the call that asked.** It belongs to the tool call, not to
  the conversation, and agent-written text never enters a user-role message.

## 3. Architecture

### 3.1 Pager replaces the accordion

A multi-question card renders exactly one question, with an `N/M` position indicator and
`‹` `›` arrows in the header. `collapsed`, `toggleCollapsed`, the height cap and the
`Collapse all` / `Expand all` control are all removed, along with their
`components.questionCard.collapse_all`, `expand_all` and `not_answered_yet` catalog keys.
A single-question card renders no pager at all — no position, no arrows — because there is
nowhere to go.

### 3.2 Submit is positional, Next carries the walk

Every question except the last offers `Next`; the last offers `Submit`. Submit is disabled
until every question is answered, so the last page can hold a disabled Submit — but no page
holds a disabled Submit as its *only* primary control, which is the state a multi-select
put the user in.

Positional and not "Submit once complete": one card is one atomic ask, so Submit belongs at
the end of the walk and nowhere else. Answering out of order therefore costs a trip to the
last page, which the arrows make one click.

`Enter` in the custom-answer box advances rather than submitting, except on the last page.
Without that, Enter would resume the agent from a page whose visible primary action is
`Next` — answer Q2 by arrow, return to Q1, retype, press Enter. Advancing also carries
focus onto the next page's input, since the pages are keyed and the focused input unmounts
with its page; an arrow *click* deliberately does not move focus, so the fix cannot
overreach into pointer navigation.

### 3.3 Answers carry their question

`asText` emits one `Q. <question>` / `A. <answer>` pair per question, pairs separated by a
blank line, via a new `components.pendingQuestionCard.qa_pair` key. A single-question card
emits the bare answer with no label, because there is nothing to disambiguate and the
wrapper would only echo a question the user just read.

The `Q.` / `A.` initials are localized per catalog (`F./A.` de, `В./О.` ru, `問/答` ja …):
the string becomes the user's own chat bubble, so an English `Q.` in a Russian transcript
reads as a defect.

§3.6 supersedes this as the path answers take to the agent. The labelled text survives in
one place only: an answer that arrives after its wait has ended is put back in the composer,
never sent, and the user chooses whether to send it as their own words.

### 3.4 What is lost, and what replaces it

Off-screen questions are unmounted, so the folded rows' one remaining benefit goes with
them: a folded row carried its own answer, so the stacked card showed every answer at
Submit time.

Replaced by half of that: a footer line naming how many questions remain unanswered, which
jumps to the first of them — except when that question is the one already on screen, where
a jump would land where the user is standing, so it degrades to plain text.

So *which* questions still need attention stays visible; *what will be submitted* does not.
Checking the answers before Submit now means paging back with the arrows. This is a real
reduction and not a like-for-like swap. §7.1 holds the summary that would restore it,
deferred to a follow-up.

### 3.5 Use cases, and what each one does

Every row is the behaviour as designed, with the test that holds it. The point of the table
is that the design is checkable rather than persuasive: a reviewer who disagrees with a row
is disagreeing with something specific, and a future change that breaks a row breaks a
named test.

| Case | Behaviour | Pinned by |
|---|---|---|
| One question | No pager, no position, no arrows. Submit is the only primary control, and the submitted text is the bare answer with no `Q.`/`A.` wrapper | `offers no pager on a single question, where both arrows would be dead`; `offers Submit alone on a single-question card`; `does not auto-advance a single-question card` |
| Two or more questions | One question on screen, `N/M` in the corner, arrows to move; the arrows stop at both ends rather than wrapping | `shows one question at a time, with the position in the corner`; `walks forward and back through the questions`; `stops at both ends instead of wrapping` |
| A single-select is answered | Auto-advances to the next **unanswered** question, so the common path costs one gesture per question — and holds still when there is nowhere useful to go | `auto-advances to the next unanswered question on a single-select answer`; `holds still on the last answer rather than jumping somewhere arbitrary` |
| A multi-select is answered | Holds position, because one click does not finish it, and `Next` carries every pick forward | `does not auto-advance a multi-select, which is unfinished after one click`; `carries a multi-select forward on Next, keeping every pick` |
| An already-answered question is changed | Does not advance away: the user came back deliberately | `does not advance away when an already-answered question is changed`; `does not auto-advance on a deselect, where the user is still choosing` |
| Questions answered out of order | Permitted. Submit appears only on the last page, so completing out of order costs one arrow click to reach it | `withholds Submit before the last question even when every answer is in`; `submits answers picked across separate pages` |
| A question on screen is unanswered | `Next` is disabled; the corner arrows are not, since review is not progress | `keeps Next disabled until the question on screen is answered`; `leaves the corner arrows ungated, since review is not progress` |
| Some question elsewhere is unanswered | Submit is disabled and the footer names how many remain, jumping to the first — as plain text, not a jump, when that question is the one on screen | `names how many questions are unanswered and jumps to the first`; `states the count without offering a jump when the outstanding question is on screen`; `drops the unanswered notice once every question is answered` |
| Keyboard only | `Enter` in the custom box advances and carries focus onto the next page's input; it submits only on the last page, and cannot skip an unanswered question. An arrow *click* deliberately does not move focus | `advances on Enter in the custom box, and submits there on the last question`; `will not submit on Enter from a page that shows no Submit`; `does not let a stray Enter skip an unanswered question`; `carries focus onto the next page when Enter advances the walk`; `leaves focus alone when an arrow moves the page` |
| The same card is re-dispatched | In-progress answers survive; a genuinely different question set resets to the first question | `keeps in-progress answers when the same card is re-dispatched`; `resets when the prompt is reused with different options`; `starts a replaced question set back at the first question` |
| A **shorter** replacement set arrives while the user is on a later page | The page is clamped into range for every consumer of it — index, position, arrows, `isLast` — so the card re-renders rather than throwing and taking the dashboard with it | `survives a replacement question set shorter than the page it is on` |
| A non-English locale | Controls and the `Q.`/`A.` initials both come from the catalog, so no card and no transcript renders in English | `catalogParity.test.ts` |

### 3.6 `ask_question` blocks, and the answers are its tool result

> **Accepted, not on main.** This section is implemented by
> [#15250](https://github.com/kirodotdev/KiroCrew/pull/15250), which has not merged.
> On main `ask_question` is still non-blocking and tells the agent to end its turn,
> and no `/api/agent-ask/*` route exists. Of the tests named below, only
> `test_non_dashboard_session_is_refused_with_options_hint` exists on main; the rest
> are planned with #15250.

This closes §1.2. The tool no longer returns at once. It opens the card on the calling
session's own attested dashboard tab and waits, and the call's result is how the card ended.
Nothing it produces is ever sent as a user message.

![Six captured frames from a live dev instance, dark mode. A three-question card shown while the ask_question tool row reads Running. The answered result: a "3 questions answered" card opened to each question and answer, with no user message. A dismissed card, and the agent reading that it was dismissed. A reply typed in the composer while the card was up, steered into the running turn as the answer. The answers card still present after a page reload. A single multi-select question answered as "1 question answered", Cheese, Olives](assets/question-card-blocking-answers.png)

Every frame is from the real build, driven through the dashboard in a recorded session.

**The answered result** is keyed by question, one line each, so the agent never pairs
answers up by position:

```
User has answered your questions:
"Which region?"="eu-west-1"
"Which tier?"="Pro"
```

The transcript folds it into a card headed "N questions answered" (pluralized per locale),
opening to each question and its answer. It is part of the tool row, so it survives a reload.

**Every other ending has its own stable result**, so the agent can tell them apart:

| Ending | Tool result | Pinned by |
|---|---|---|
| Answered | `User has answered your questions:` plus one `"<question>"="<answer>"` line each | `test_answers_come_back_as_the_tool_result`; `reads each question and answer from an answered result` |
| Dismissed | `The user dismissed the question card without answering.` | `test_each_ending_is_told_apart` |
| User typed in the composer instead | `The user replied in chat instead of answering the question card. Their message follows as the next user message …` The typed text arrives as the user's own words, and the card is dismissed, never submitted with that text | `test_answer_endpoint_marks_a_composer_reply`; `dismisses rather than submitting the typed text as the answer`; `resolves a QUEUED send too, so the two cannot deadlock` |
| Not answered in time (default 30 min, at most 1 h) | `The user did not answer the question card in time, so it was withdrawn …` | `test_coordinator_expires_at_the_deadline_and_retires_the_card` |
| Gateway restarted, or the tab was reset | `The question card was withdrawn before the user answered …` | `test_a_forgotten_ask_reports_withdrawn_instead_of_hanging`; `test_slot_reset_withdraws_an_agent_ask` |
| The tool call was cancelled | The card is withdrawn from the screen | `test_cancellation_withdraws_the_card` |
| No card can be shown (not a dashboard session, or no window open) | `The question card could not be shown: <why>. Ask in plain text instead …` | `test_non_dashboard_session_is_refused_with_options_hint`; `test_no_attached_client_says_the_card_could_not_be_shown` |

**There is no non-blocking fallback.** A fallback would be the §1.2 path by another name.
When a card cannot be shown the result says why, and the agent asks in plain text.

**Staying alive while it waits.** The tool waits in 20-second slices and pings the session
keepalive before each one, as the `wait` tool does, so the 600 s tool-stall watchdog never
fires on a long answer. A wait that stops polling for 120 s is presumed dead and its card is
retired. A few transport errors in a row are retried before the tool reports the card
withdrawn.

**A late answer is not lost.** If the user submits after the wait has ended, the server
answers 404, and the labelled §3.3 text is put back in the composer for the user to send or
discard. It is never sent on their behalf.

## 4. Migration plan

No migration. The card is client-side and mounts per delivery; there is no persisted card
state, no stored preference for the accordion, and no API shape change — the
`question_card` websocket frame and `POST /api/ask-question/<id>/answer` are untouched. The
only wire-visible change is the text a stateless card submits (§3.3).

The three retired catalog keys are removed from all 12 catalogs in the same change, so no
locale renders a key that no longer exists and none renders the new controls in English.

§3.6 does change a contract: the one between `ask_question` and every agent that calls it.
It is accepted but not on main (#15250), so the steps below are planned:

- The tool description, `prompt.md` and the per-turn nudge drop "END YOUR TURN after
  calling" and say the call returns the answers. An agent following the old text still
  works; it just ends a turn it did not need to.
- `ask_question` is no longer a session directive. Its applier, its dashboard-only entry
  and the chat runner's terminal-question handling are removed.
- Three internal routes are added, `POST /api/agent-ask/open`, `/{id}/wait` and
  `/{id}/withdraw`. They are callable only with the MCP server's internal secret and an
  attested session key. The user-facing `POST /api/ask-question/<id>/answer` keeps its
  shape and gains a `composer` marker for the reply-in-chat ending.

No stored state migrates. An ask lives in gateway memory for its wait, and a gateway restart
ends it with the withdrawn result.

## 5. Security model

The pager itself changes nothing security-relevant. It renders options the agent supplied
and sends back a string the user chose.

§3.6 removes a real exposure. Before it, agent-written question text arrived in a user-role
message (§1.2), so a later turn could treat something the agent wrote as the user's
instruction. Now the answers are the tool result, the agent's text stays in the agent's
own call, and only the answer the user picked or typed is quoted back. The quoted question
is the server's stored copy, not whatever the client sent (`test_only_the_servers_copy_of_a_question_is_quoted`).

The new wait routes are scoped to the caller:

- They require the MCP server's internal secret, so a browser cannot call them
  (`test_open_refuses_a_caller_without_the_internal_secret`).
- They require an attested session key, and an unattested caller never opens a blocking
  ask (`test_open_refuses_an_unattested_session_key`,
  `test_unattested_caller_never_opens_a_blocking_ask`).
- An ask opens only on the caller's own slot, and waiting on another session's ask returns
  not-found, so one session cannot read another's answers
  (`test_open_addresses_the_callers_own_slot_and_clamps_the_window`,
  `test_wait_for_another_sessions_ask_is_not_found`).
- The wait window is clamped to one hour, so an agent cannot hold a card open
  indefinitely.

One adjacent property is worth stating because the pager makes it easier to get wrong: a
card is replaced in place per slot, so a shorter replacement question set arriving while the
user has walked into a later page would index past the end of the new array. That is a
crash, not a leak, and it is pinned by a no-throw test rather than left to the render-phase
reset.

## 6. Non-goals

- **A review step.** Not a wizard with a confirmation page; see §7.1.
- **Partial submission.** One card is one atomic ask (§2).
- **Reordering or skipping questions the agent asked.** The pager changes presentation
  order of arrival, not the set.
- **Anything about the single-question card.** It keeps its current shape, minus the
  now-pointless label wrapper.
- **Blocking questions outside the dashboard.** Slack, cron and other surfaces have no card,
  so the tool says it cannot show one and the agent asks in plain text or with `[OPTIONS:]`.

## 7. Questions, and how they were answered

### 7.1 Restoring the pre-submit answer review — deferred

**Decided 2026-09-24 by buluoray: the pager is accepted, and restoring the answer review is
deferred to a follow-up. It does not block
[#13196](https://github.com/kirodotdev/KiroCrew/pull/13196).**

The design's one real subtraction (§3.4). A compact answered-summary above Submit on the
last page would restore it without re-stacking the card: every answer visible, read-only,
on the page where Submit already lives. It is not part of this design because it
re-introduces a variable-height region on one page, and the smaller card is worth having on
its own first.

### 7.2 Whether `Enter` on the last page submits — open

§3.2 has it submit. The argument for requiring the button is that Enter's meaning then never
changes between pages; the argument against is that it costs the keyboard walk its last
step. Nothing else in §3 depends on which way this goes, so it can be settled on its own.

## 8. Alternatives considered

**Fix the three defects and keep the accordion.** The narrowest change, and it genuinely
works for the answer-format defect, which is why it is the alternative to beat: the fix in
§3.3 is independent of the pager and could ship alone. Rejected as the whole answer because
the mode cue and the forward affordance both need vertical room per question, and the
accordion is already compensating for not having it — three mechanisms plus a control, and
a fourth compensation would be needed for the taller rows. This RFC takes the position that
the compensation is the problem.

**Accordion with a tighter viewport cap.** Cheaper still, and it is what the card's
existing `max-h` cap already is. Rejected: the cap makes the card scroll internally inside
a chat column that also scrolls, and nesting those is what made the folded state necessary
in the first place.

**A wizard with an explicit review page.** Pager plus a final read-only summary page, which
would answer §7.1 outright. Rejected for now as more card than the common case deserves —
most `ask_question` calls carry one or two questions, and a mandatory extra page taxes all
of them to serve the rare four-question card. The §7.1 summary is the cheaper half of this.

**Keep Submit always visible, enabled once complete.** Rejected: it puts a disabled Submit
on every page as the only primary control, which is the multi-select strand this change
exists to fix, and it makes "where does the ask end" a function of state rather than
position.

**Keep answers as a user message, and only fence the agent's text.** Wrap the quoted
questions in a marker the model is told to distrust. Rejected: it still puts agent text in
a user-role turn and relies on the model honouring the marker, and it does nothing for the
other two §1.2 problems. The answer is not tied to its call, and a dismiss is still silence.

**Blocking with a non-blocking fallback.** Block where a card can be shown, and fall back to
today's path elsewhere. This was the first implementation. Rejected: the fallback is exactly
the §1.2 path, so every session without a confirmed identity would keep the exposure, and
the agent would have two contracts to handle.
