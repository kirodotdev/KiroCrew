---
title: Conductor context cap -- the gateway trims a goal conductor's KAS chat to 150K tokens at a turn start
status: draft
author: iamwhatever, with kirocrew-worker
created: 2026-10-19
last-audited: 2026-10-19
audited-at: c3f2b27de7
doc-pr: null
implementation-prs: []
tracking-issues: [22082]
supersedes: [rfc-conductor-rolling-reset.md]
superseded-by: []
---

# RFC: Conductor context cap

- Status: draft. Two decisions are pending, and both come before the
  implementation pull request (section 9): acceptance by the user
  (iamwhatever), the maintainer who owns the conductor patrol, and agreement
  from bolichen97, whose ruling on
  [#18931](https://github.com/kirodotdev/KiroCrew/issues/18931) this document
  revisits for goal conductors (section 4). It lands on its own first, because
  the First Principles lane reads an RFC's status off the base branch, and the
  implementation changes what a goal conductor's session does by default.
- Supersedes [rfc-conductor-rolling-reset.md](rfc-conductor-rolling-reset.md),
  a draft that was never accepted. That draft moved the conductor's own
  `reset_conversation` call to a new point in its prompt. The user decided on
  2026-10-19 that a prompt rule cannot bound the context, and that the gateway
  bounds it instead.
- Leaves [rfc-conductor-round-reset.md](rfc-conductor-round-reset.md)
  (implemented) as it is: the conductor's own round-close reset stays, and the
  cap does not depend on it.
- Related: [#11377](https://github.com/kirodotdev/KiroCrew/pull/11377) (open)
  proposes the same restart-and-replay move as an operator's choice at Crew's
  own compaction threshold, which a KAS session never reaches (section 3.7).
- Tracking issue: [#22082](https://github.com/kirodotdev/KiroCrew/issues/22082).
- Code measured at `c3f2b27de7`. The numbers come from the R5 study on one
  operator's host: 5,174 dashboard turns from 2026-10-01 to 2026-10-19, each
  joined across Kiro Crew's usage rows, its chat history and the KAS
  transcripts, plus one live probe on a throwaway KAS session. Every curve is
  Claude Opus 5.5 on KAS with a 1M-token window.

## 1. Problem

A goal conductor's chat only grows. On KAS, Crew leaves compaction to the
engine, which summarizes at 80% of the window, so the conductor's crewmate DM
climbs from about 55K to about 800K tokens, then drops to a summary, then
climbs again (about 28K tokens an hour). Every request re-sends the whole chat.

| Conductor DM, 797 turns in R5's join | Value |
|---|---|
| Context at a turn's first request, p50 / p90 | 443K / 727K |
| Turns that start with the cache cold (5 min or more since the previous turn, or a new session) | 64% |
| First visible output on a cold request, 443K vs 150K | 14.3 s vs 7.1 s |
| Credits for a cold first request, 443K vs 150K | 5.60 vs 1.93 |
| Credits on a median active day | 660 |

The cold request is what a person waits on, and its cost grows with the
context: first visible output rises 2.47 s per 100K tokens when the cache is
cold and 0.54 s when it is warm.

Two existing levers do not reach it:

- **The conductor's own reset.** The prompt asks for `reset_conversation` at a
  round close. A conductor that refills each seat as an item lands rarely
  reaches one. The DM's transcript holds 822 turns since 2026-10-07: no call
  in the first 682, 6 in the next 39 (a day and a half), and none in the 101
  since.
- **KAS's own compaction.** It keeps only a summary, lands at 32K to 50K
  whatever the target, and runs inside the turn with the session's own model
  and effort: 77 s to 404 s at Opus 5.5 max (p50 224 s). The manual verb
  (`_kiro/session/compact`) hit KAS's 300-s limit on a 325K session at max
  effort in the probe and compacted nothing. KAS 2.32.0 also has no setting
  for a longer cache life.

## 2. Goals and non-goals

Goals:

- A goal conductor's KAS chat stays between 150K and 400K tokens, with no model
  call to trim it and no wait inside a turn.
- What the conductor needs after a trim comes from what it already keeps: its
  session ledger, its work ledger and the newest messages of its chat.
- Nothing is deleted. The transcript a person reads stays whole, and older
  content stays searchable.

Non-goals:

- Normal chats, workers and other crewmates. They keep no ledger, so their
  summary needs a model call, built off the hot path before the cache goes
  cold. A later RFC covers them.
- Other backends. They keep `session.autocompact_pct`.
- Channel surfaces (Slack, Discord and the others). Phase 1 is dashboard turns.
- Changing KAS: its 80% summarize and 95% truncate stay, and so does the
  manual `/compact` through `_kiro/session/compact`.
- Any change to `reset_conversation`, its grant or its admission checks, and
  any new `reset_conversation` call by the conductor.
- A timer. A trim only ever happens at the start of a turn that is about to
  run anyway.
- A context-size hint in the wake, and a longer cache life.

## 3. Design

### 3.1 Which sessions

A session is capped when all of these hold:

| Check | Source |
|---|---|
| its slot runs the goal conductor, `kirocrew-conductor` or its deprecated alias | `WAKE_RESET_AGENTS` in `src/kiro_crew/autonudge_selfarm.py` |
| its backend is KAS | `ACP_BACKENDS_HARNESS_MANAGED_COMPACTION` in `src/kiro_crew/agent_sdk/backends_turns.py` |
| the turn is a dashboard turn | `_run_chat` in `src/kiro_crew/dashboard/chat_runner.py`: the conductor's DM, or a sidebar chat that runs the conductor |
| the cap is on | `session.context_cap`, default `true` (section 3.6) |

Every other session keeps today's behaviour.

### 3.2 Marks and rules

| Name | Value | Why |
|---|---|---|
| LOW | 150K tokens | where a trim lands. A cold request there takes 7.1 s, about what a warm 400K request takes today (7.0 s) |
| HIGH | 400K tokens | above it a cold request passes 13 s, and a trim from 400K to 150K pays back in 3.4 requests, about one conductor turn |
| Rule B line | 200K tokens (LOW + 50K) | at LOW itself Rule B fires on 63 of 100 conductor turns; at 200K on 12, keeping 94% of the gain |
| Cache life | 300 s | credits per request step from 1.88 to 2.95 at 5.0 min at the same size |

The two rules, read at the start of each turn:

- **Rule A:** the context is over HIGH. Trim to LOW.
- **Rule B:** the previous turn on this session ended 300 s or more ago, and the
  context is over 200K. Trim to LOW. The cache has already expired, so the
  trim costs nothing extra: the next request writes a new cache either way.

The inputs:

- **Context** is the session's last confirmed reading: `context_usage_pct()`
  times the model's window (`window_for_provider_client`), taken from the
  previous turn's last request. A reading that `_context_pct_is_unknown`
  reports as unknown never trims.
- **The clock** is `_Session.last_used`. A turn sets it when it acquires the
  session, and `SessionAllocationService.release` sets it again when the turn ends,
  so at the check, which runs before the next turn acquires, it holds the
  previous turn's end. The cache life counts from the start of the last
  request, which comes before the turn ends, so this clock never calls a warm
  cache cold. Every other writer (`touch()`, a provider swap, another
  surface's acquire) stamps a later moment, which can only read warmer. R5's
  replay of the real sequences used the same clock, the gap from the previous
  turn's end.
- **Damping:** a session that a trim started is not trimmed again until its
  reading has grown 50K past its own first reading. A trim that lands higher
  than planned cannot then repeat on every cold turn.

The marks are fixed in code for phase 1. They are tokens, not a share of the
window: on a model whose window makes KAS compact before 400K, the cap simply
never fires.

### 3.3 The trim

**Where.** At a turn start, before the turn acquires the session: the point in
`_run_chat` where `_consume_pending_reset` already applies a queued project
change and `_retire_sessions_on_identity_change` already retires a session
whose account changed.

**How.** `discard_conversation(key, replay=True, skip_if_busy=True)`: the
teardown `reset_conversation` and the `/api/chat/slots/{slot}/reset-conversation`
route use, with replay kept on, which the route already lets a caller choose.
The KAS conversation ends; the slot, its channel links and its transcript
stay. The turn then acquires a new KAS session, and its first
prompt is the seed. If the teardown refuses, nothing changed and the turn runs
on the old session.

**The seed**, built where every cold start builds its history today
(`_session_replay_history` in `src/kiro_crew/dashboard/chat_turn/prompt_assembly.py`):

1. Kiro Crew's usual new-session context: agent prompt, member section,
   memory, skills and steering. With KAS's own system prompt and tools, a
   fresh conductor session starts at about 55K tokens.
2. A trim note for the model: the chat was trimmed at this turn's start, the
   newest messages follow as text, tool output and older messages are not in
   context, and `search_chat_history` finds them in the transcript.
3. The summary: the session-ledger snapshot (`render_snapshot` in
   `src/kiro_crew/session_ledger.py`, at most 1,600 chars: goal, phase, next,
   tried, artifacts). A patrol wake already carries the same block
   (`compose_nudge_body`), so on a wake it appears twice, once per trim. The
   work ledger is not copied: the conductor's prompt already has it read the
   ledger first, every cycle.
4. The tail: `build_session_replay` over the slot's transcript with today's
   row rules (user, assistant and inject rows, newest first; no tool rows; at
   most 500 user and assistant rows and 10 inject rows, each inject row
   clipped to 2,000 chars, and inject rows held to a quarter of the budget),
   under a trim budget of **270,000 chars** instead of
   the 80,000-char `_REPLAY_BUDGET_CHARS`. Every other cold start keeps
   80,000.
5. The turn's own message.

No model call is spent: the summary is ledger state on disk, and the tail is
text already in the transcript.

**Choosing N, the tail.** The budget fills the room LOW leaves:
(150K − 55K base − about 5K for the note, the summary and the turn's message)
× 3 chars per token ≈ 270,000 chars. R5 put that room at about 95K tokens,
which is about 19 conductor turns of KAS context growth. That growth counts
tool output, which the replay never carries, so a text tail reaches much
further back. Measured on the same conductor DM transcript (822 turns since
2026-10-07), with today's row quotas, walking back from 30 points spread over
its last 300 turns:

| Tail budget | Turns held, p50 (min) | Hours covered, p50 (min) | Session lands near |
|---|---|---|---|
| 80,000 chars, today's replay | 63 (29) | 35 (10) | 80K |
| **270,000 chars, the trim** | **205 (163)** | **101 (86)** | **150K** |

At every point the char budget bound first: the 270,000-char tail held 408
user and assistant rows at the p50, under the 500-row quota. The conductor's
replayable text is about 630 chars per turn at the p50 (564 on a patrol turn,
1,123 on a person's turn). R5's probe seeded a new KAS session,
with no Kiro Crew context, with a summary and a 330K-char text tail; it landed
at 152K, and its credits matched the cost model (1.87 measured vs 1.93
predicted).

| | What happens to it |
|---|---|
| Kept in context | the session context; the trim note; the session-ledger snapshot; the newest messages as text, about 205 turns |
| Dropped from context | every tool call and its output; messages older than the tail; the old KAS session |
| Kept on disk | the slot's transcript, `~/.kiro/crew/sessions/dashboard_<slot>.jsonl`, untouched, which is what the dashboard shows; the work ledger and the session ledger; the old KAS session's folder under `~/.kiro/sessions/` |
| How it is found again | `search_chat_history` searches the transcript and `get_chat_session` reads it (both are on the conductor's mounted `@kirocrew-core`); the conductor's shell can grep the file |

### 3.4 When a trim never fires

Each check below stops the trim. Nothing changes, and the next turn start
checks again.

| Check | Why |
|---|---|
| the session is out of scope (3.1), or the cap is off | phase 1 is goal conductors on KAS |
| no live session, or its reading is unknown | a session that has not reported cannot be measured |
| the turn is a slash command (`/compact`, `/clear` and the like) | those manage the session themselves |
| a `reset_conversation` is already queued on the slot | it lands at this turn's end and drops more |
| a question card or tool approval is pending in the slot (`_slot_has_pending_interaction`) | the answer would reach a chat that no longer holds the question |
| sub-agents are attached: running, queued or delivering a result (`subagents_attached_async`) | the teardown releases the runtime they run on |
| another turn holds the session, such as a channel turn on a linked session | `discard_conversation(..., skip_if_busy=True)` refuses under the same lock that pops the session, so a probe and a teardown cannot be split by a turn that starts in between |
| neither rule holds, or the damping guard holds | nothing to trim |

A trim never fires mid-turn: the check runs only before a turn acquires the
session.

### 3.5 What the person sees

One transcript row per trim, in the compaction notice family and in the shape
of `_AUTO_RESTART_UNCOMPACTABLE_NOTICE` (`src/kiro_crew/dashboard/state_parts/session_events.py`),
for example "♻ Context reached 412K tokens, so the session was restarted with
about 150K." followed by `_RESTART_MEMORY_TAIL`: "The conversation above is still
here, and the agent's next reply starts from a recent excerpt of it rather
than the whole thing." The row carries `meta.kind = "compaction"` and its own
`meta.notice`, as the watchdog's recycle notice does, so the existing
`CompactionCard` draws it with no frontend change. It is a display row. The
replay keeps assistant rows of the compaction family today, so the trim's
replay skips rows that carry this notice, and a later trim spends none of its
tail on them. The model learns about the trim from the note in its seed.

### 3.6 Off switch

`session.context_cap`, a boolean, default `true`. Off, every session behaves as
it does today. The marks stay in code until a later phase has a reason to tune
them per chat type.

### 3.7 Unchanged

- KAS's compaction thresholds, and the manual `/compact` through
  `_kiro/session/compact`.
- `session.autocompact_pct` and the turn-end compaction gate.
  `CompactionCoordinator._gate_rung` still declines a KAS session as
  `compact_unsupported`; the cap does not run through it, because Rule B needs
  the gap before the next turn, which the end of a turn cannot know.
- The round-close reset: its grant, its admission checks
  (`_refuse_unvouched_wake_reset`) and the prompt and skill text.
- Every other agent, backend and surface, and the 80,000-char replay of every
  other cold start.

## 4. #18931: why Crew needs a cap on KAS now

On 2026-10-12, bolichen97 ruled on
[#18931](https://github.com/kirodotdev/KiroCrew/issues/18931): on KAS, Crew keeps
the engine's own compaction thresholds and does not override them, and
`session.autocompact_pct` applies only to backends Crew manages itself.
[#19332](https://github.com/kirodotdev/KiroCrew/pull/19332) documented it.

This RFC asks to revisit that ruling for goal conductors, for four reasons:

1. **The engine's threshold sits too high for a patrol.** At 80% of a 1M window,
   a conductor runs at 443K tokens at the p50 and 727K at the p90 between
   compactions. 64% of its turns start cold, and a cold first request at 443K
   takes 14.3 s and 5.60 credits, against 7.1 s and 1.93 at 150K.
2. **The engine's compaction cannot serve as a cap.** It keeps only a summary,
   lands at 32K to 50K whatever the target, takes no instructions, and costs
   77 s to 404 s inside the turn at Opus 5.5 max. The manual verb timed out at
   300 s on a 325K session in the probe.
3. **No engine setting helps.** KAS 2.32.0 exposes no cache life setting. Even
   a 1-hour cache would save 3.55 credits per conductor wake, against 6.32 for a
   trim to 150K.
4. **The gain is large.** Replaying the conductor DM's real sequence of turns
   under the cap cuts the context part of its credits by 63% (10.24 to 3.80 per
   turn), about 360 credits on a median active day, and its mean first visible
   output from 12.1 s to 7.0 s.

What the cap leaves alone: it never calls KAS's compaction and never changes
KAS's thresholds, which stay as the engine's own backstop for every session.
Under the cap a conductor stays far below 80%, so the engine does not compact
it. `session.autocompact_pct` still does not apply on KAS.

The cap still reverses #18931 for one scope, Crew bounding a KAS session's
context, so it needs two agreements: bolichen97's, as the owner of that ruling,
and the user's (iamwhatever), as the maintainer who owns the conductor patrol.

## 5. Measures for the implementation

**Targets.** R5's replay of the conductor DM's real sequence (LOW 150K, HIGH
400K, Rule B at 200K), priced with the same fits on both sides:

| Measure, conductor DM | Today, in the replay | Target |
|---|---|---|
| Trims per 100 turns | 0 | about 12 |
| Context at a turn's first request, p50 | 444K | 175K |
| First visible output, mean | 12.1 s | 7.0 s |
| First visible output on a cold turn, mean | 14.9 s | 7.6 s |
| Credits per turn, context part | 10.24 | 3.80 (−63%) |
| KAS compactions | one every 20 to 49 hours | none |

The replay keeps only the Opus 5.5 turns that have a size on every request,
so its baseline reads 444K where section 1's wider join reads 443K.

R5 times first output from the moment the prompt reaches KAS. A trimmed turn
also starts a new KAS session, measured on the same host at 4.4 s p50 (1.9 s
spawn plus 2.5 s `session/new`) plus 0.8 s of context build. On 12 turns in
100, that adds about 0.6 s to the mean.

**Before/after check.** Same host, same conductor DM. Before is R5's window,
2026-10-01 to 2026-10-19. After is the first 7 days with the cap on. Both use
the same joins: each dashboard usage row matched to the KAS `usage_summary` row
with the same credits within 60 s; the context at a turn's first request is
its first `contextUsage` reading times the window; first visible output is the
earlier of the first streamed chunk (`turn_stats.ttft_ms`) and the first tool
row; a turn is cold when the previous one ended 5 min or more before it.
Trims are counted from their notice rows, and a trim's landing is the new
session's first reading.

It passes when, over the after window:

- context at a turn's first request is 200K or less at the p50;
- a trim lands between 125K and 160K at the p50;
- there are 20 trims per 100 turns or fewer;
- first visible output on a cold turn is 10 s or less at the p50, against
  15.8 s measured today (R5's 7.6 s mean, plus a new session's start on the
  trimmed turns);
- credits per turn are at least 40% lower at the p50 (the replay's 63% is the
  context part; it is about 55% of the conductor's daily total);
- KAS compacts the conductor DM zero times;
- in every trim of the first week, read by hand, the next reply neither asks
  again for something already answered nor misses a pending decision.

**Tests in the implementation.** Each rule and each check in section 3.4 gets
a test that fails first: Rule A, Rule B, the 300-s clock read from the
previous turn's release rather than its acquire, the damping guard, an unknown reading, a slash command, a queued
reset, a pending question, attached sub-agents, a refused teardown and the off
switch. The seed gets tests for its budget, its note, its snapshot and the
notice row the trim's replay skips.

## 6. Risks

- **What a trim forgets.** Tool output and text older than the tail leave the
  model's context. The ledgers hold the conductor's durable state (about 3K
  tokens per wake), the tail holds about 101 hours of conversation at the p50
  and 86 at the least, and the transcript holds the rest. R5 did not measure
  quality beyond one planted fact recalled after a seeded start; the hand
  check in section 5 covers it.
- **A slower trimmed turn.** The new session's start (4.4 s p50) lands on the
  trimmed turn. A Rule A trim on a warm turn makes that turn slower than it
  would have been, and every later turn faster.
- **A landing off target.** Chars per token vary with the text, so a trim may
  land above or below 150K. The damping guard stops a high landing from
  repeating, and the before/after check measures the landing.
- **A lookup that asks first.** `search_chat_history` and `get_chat_session`
  are mounted on the conductor but are not in its auto-approve grants
  (`_CONDUCTOR_CORE_GRANTS`). On a slot that asks before tools run, a lookup
  waits for the person.

## 7. Security

The trim adds no tool, no grant and no admission path. It runs in the gateway,
reads only the slot's own transcript and session ledger, the same data every
cold start's replay and every patrol wake already read, and the tail goes
through `build_session_replay`'s existing redaction of credentials and
exfiltration URLs. It ends only the slot's own KAS conversation, through the
same teardown `reset_conversation` uses, and never touches another slot, the
transcript on disk or either ledger.

## 8. Alternatives considered

- **KAS's compaction verb at the marks.** It keeps only a summary, lands at
  32K to 50K instead of 150K, and runs minutes inside the turn (section 1).
- **Crew's threshold on KAS through `session.autocompact_pct`.** It would send
  KAS the same compaction verb, with the same costs.
- **The conductor resets itself** ([rfc-conductor-rolling-reset.md](rfc-conductor-rolling-reset.md)).
  A prompt rule depends on the model choosing the moment; the DM made no call
  in its first 682 turns.
- **A 1-hour cache.** KAS exposes no setting for it, and in Kiro credits a trim
  saves more per wake (6.32 vs 3.55).
- **Rule A at turn end, Rule B on an idle timer.** Two trigger points and a
  timer, for the same effect as one check at the next turn start.
- **Keep today's 80,000-char tail.** The session would land near 80K with 63
  turns (35 hours) of text. That is a lower LOW than the one this RFC sets; it
  stays a later option if the before/after check shows 150K holds more than the
  conductor uses.
- **A model-written summary.** It costs a model call, read with the cache
  already cold under Rule B. The conductor's ledgers make it unnecessary.
- **Wait for #11377.** Its rotations run at `session.autocompact_pct`, which
  declines KAS, so a KAS conductor never reaches them. If it lands first, the
  trim can call its restart instead of `discard_conversation`.

## 9. Decisions needed

1. **Acceptance** by the user (iamwhatever), the maintainer who owns the
   conductor patrol. On acceptance the status becomes `accepted`, with the
   decision and its date recorded here.
2. **Agreement from bolichen97** to revisit #18931 for goal conductors on KAS
   (section 4).

Both come before the implementation pull request opens.

## 10. Rollout

**Phase 1, this document.** One implementation pull request:

- the rule check at a dashboard turn's start, reading the existing context
  meter and `_Session.last_used` (3.2 to 3.4);
- the trim's tail budget, note and snapshot in the seed (3.3);
- the notice row (3.5) and `session.context_cap` with its configuration row
  (3.6);
- the tests in section 5.

Exit criteria, each testable: a capped session over 400K at a turn start
starts that turn on a new KAS session whose first prompt carries the note, the
snapshot and a tail of at most 270,000 chars; a session over 200K after 300 s
does the same; every check in 3.4 leaves the old session in place; the
transcript on disk is byte-identical before and after a trim, apart from the
notice row; and with `session.context_cap` off, no session changes behaviour.

**Phase 2, a later RFC.** Normal chats, workers and other crewmates, with a
summary built off the hot path before the cache goes cold: a small model over
Kiro Crew's own text history, or a summary request that shares the turn's
cache right after the turn ends.
