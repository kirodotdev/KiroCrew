---
title: Worker context cap -- the gateway trims a dispatched worker's KAS chat on an expired cache, with a 600K safety line
status: draft
author: iamwhatever, with kirocrew-worker
created: 2026-10-22
last-audited: 2026-10-22
audited-at: ba317c8f61
doc-pr: null
implementation-prs: []
tracking-issues: [23037]
supersedes: []
superseded-by: []
---

# RFC: Worker context cap

- Status: draft. Acceptance by the user (iamwhatever), the maintainer who owns
  the crew's conductor and workers, comes before the implementation pull
  request (section 10).
- Phase 2 of [rfc-conductor-context-cap.md](rfc-conductor-context-cap.md),
  whose phase 1 shipped in
  [#22718](https://github.com/kirodotdev/KiroCrew/pull/22718). That document's
  section 10 names workers as the next phase. This one reuses its check, its
  trim and its holds, and changes three things for workers: which sessions are
  in scope, the marks, and the seed.
- Tracking issue: [#23037](https://github.com/kirodotdev/KiroCrew/issues/23037).
- Code read at `ba317c8f61`. Numbers come from one operator's host: 887
  dashboard turns of 42 dispatched-worker KAS sessions, 2026-10-17 to
  2026-10-21 (4.5 days), the R7 join of Kiro Crew's usage rows, the chat
  transcripts and the KAS transcripts. Every turn is Claude Opus 5.5 on KAS,
  1M-token window. No live spend. Scripts and outputs:
  `research/crewmate-perf/r8/` in the operator's crew workspace.

## 1. Problem

A worker's KAS chat only grows, and a worker re-reads all of it on every
request. Its tool output is most of it.

| Dispatched worker, 887 turns | Value |
|---|---|
| Context at a turn's first request, p50 / p90 | 508K / 728K |
| Turns that start with the cache cold or on a new session | 64% |
| Cold first visible output, p50 | 15.8 s |
| Context part of credits per turn, mean | 23.5 |
| Requests in a turn, p50 / p90 (R5, 10-01 to 10-19: first turn; later turns) | 66 / 149; 2-3 / 17-28 |
| KAS's own summarizations (80% of the window) | 9, in 7 of 42 sessions |

Pricing is R5's (R5-context-cap section 4): a request costs about
0.048 + 0.452 credits per 100K tokens of context, plus 0.802 per 100K when the
cache is cold. First visible output is 4.87 + 0.54x s warm and
3.39 + 2.47x s cold (x in 100K tokens). The cache lives 5 minutes from the
start of the last request.

## 2. Goals and non-goals

Goals:

- A dispatched worker's KAS chat is trimmed at a turn start once the cache has
  already expired and the chat is over 200K, and at any turn start over 600K.
- No model call, no wait inside a turn, nothing deleted: the transcript stays
  whole and searchable.
- A trimmed worker knows its item, its last report and where its work is, and
  re-grounds from its worktree and its PR.

Non-goals:

- Normal Sessions-sidebar chats and other crewmates. The user ruled on
  2026-10-22: workers first, normal chats later.
- Mid-turn trims (section 6).
- Other backends, channel surfaces, and any change to KAS (section 7).
- Building the trimmed session off the hot path. It is a follow-up
  (section 5.4).

## 3. What a trim drops and what it costs

### 3.1 What leaves the context

At a would-be trim, under the recommended marks (section 4), split by kind
from the KAS transcripts. Tokens per char come from a median regression of the
context meter's per-request steps over 9,570 steps: 0.356 per tool-result char,
0.202 per tool-call argument char, 0.90 per reasoning char.

| | Value |
|---|---|
| Context dropped per trim, p50 | 286K tokens |
| of it: tool calls and their output | 53% |
| reasoning | 30% |
| per-request overhead | 14% |
| conversation text | 3% |
| Text the chat had written before the trim, p50 (max on the host) | 4K tokens (34K) |
| Where the trim lands, p50 (p10, p90) | 53K (51K, 59K) |

A worker's conversation text is small: its transcript holds 4.7K tokens of
user, assistant and inject rows at the p50 (13.6K at the p90), against 339K
chars of tool output. So **the text tail never fills its budget**. In 0 of 54
trims did the tail stop at its budget, and every trim kept the chat's whole
text. The landing is a fresh worker session's base (46K tokens, R7's first
message; the first reading of 50 worker sessions is lower, 36K at the p50), the seed's
head (about 3K, plus up to 0.5K for the edited-file list) and that text. A lower or higher LOW does not change it.

This is also why R7's phase-2 replay (`cap_replay_r7data_phase2.out`: 92
trims, 338 damped) differs from this one: it landed every trim at 187K, the
conductor's tail. At 53K, the damping floor (first reading + 50K) sits below
the 200K Rule B line, so it never holds.

### 3.2 The redo it causes

A proxy, read from the tool calls after each would-be trim, up to the next
trim:

- **Files**: a file the worker edits, or names in a tool call, after the trim
  whose last read came before it (and after the previous trim), with no re-read
  in between. The trimmed worker would read it once more.
- **Commands**: a state command (`git status|log|diff|show`, `gh pr|run|api`,
  `pr_status.py`, `pr_findings.py`) it ran in the 3 turns before the trim and
  runs again later, but not in the first turn after. The trimmed worker would
  run it once more.
- **Price** of each: one extra request at the trimmed context, plus the re-read
  tokens on every request up to the next trim. Time: one request, 10.9 s p50.
- **Upper bound**: every file read before the trim that is touched again at
  all is re-read whole.

| Per trim, recommended marks | Proxy | Upper bound |
|---|---|---|
| Files re-read | 0.61 | 2.72 |
| Commands re-run | 0.04 | 0.04 |
| Tokens re-read | 1.1K | 3.2K |
| Credits | 0.65 | 2.37 |
| Time | 7 s | 30 s |

Against about 194 credits saved per trim (section 4), the redo is under 1.3%
even at its upper bound.

### 3.3 The prompt cache

Each trim is classed by the gap from the previous turn's **last request
start** to this turn's start, not by its rule. A turn that is cold and over
the Rule A line is labelled A.

| Marks | Trims | On a warm cache | On an expired cache | Labelled A but expired |
|---|---:|---:|---:|---:|
| 150/400 (the conductor's) | 59 | 17 | 42 | 1 |
| 250/600 | 40 | 6 | 34 | 0 |
| **Cold-only, Rule B 200K, safety 600K** | **54** | **6** | **48** | 0 |
| Cold-only, Rule B 200K, safety 700K | 54 | 4 | 50 | 0 |
| Cold-only, Rule B 300K, safety 700K | 40 | 4 | 36 | 0 |

Every Rule B trim is on an expired cache. Rule B measures from the previous
turn's end, which is after its last request started.

**A trim on a warm cache**, priced: the trimmed turn pays one new-session
start and a cold write at the landing, 0.048 + 1.254 × 0.53 = 0.71 credits,
instead of a warm read of the old context, 0.048 + 0.452 × 6.6 = 3.03 credits
at the recommended set's 660K p50. Each later request then saves
0.452 × (C − L).

| Warm trim, recommended marks | Value |
|---|---|
| Payback, in requests | 1: the first request is already cheaper |
| Credits saved on that turn alone, p50 | 11.2 |
| That turn's first output, p50 | +1.5 s (start 5.3 s) to +4.8 s (start 8.6 s) |

**The start of a trimmed session, measured.** A trim's next turn opens a new
KAS session on its send path: spawn, `session/new` with Kiro Crew's MCP stubs,
the context build (memory recall included) and the replay. The nearest real
case is a turn that opened a new KAS session for a chat that already had one
(a reset, a restart, an idle reap). From the dashboard opener to the prompt at
KAS:

| Turn | n | p50 | p90 |
|---|---:|---:|---:|
| New session, chat had one before | 7 | **5.3 s** | 23.0 s |
| New worker chat, dispatched, live code since 10-21 | 2 | 8.6 s | 8.7 s |
| New worker chat, dispatched, before 10-21 (14.8 s memory recall seen) | 34 | 23.3 s | 33.1 s |
| Same session, cold | 527 | 0.1 s | 0.3 s |

R7 assumed 5.2 s. This RFC uses 5.3 s, and gives 8.6 s as the high case.

### 3.4 The invariants that keep the cache whole

1. A trim happens only at a turn start, before the turn claims its session.
   Nothing changes the context inside a turn.
2. Between trims, history is append-only: Kiro Crew never edits or removes a
   message the KAS session holds.
3. There is no sliding window. A trim replaces the whole session once, and
   the new session is append-only again from its first request.
4. A cold trim (Rule B) happens only after the cache has expired: 300 s since
   the previous turn ended, so more than 300 s since its last request started.
5. A warm trim happens only over the 600K safety line, where KAS's own
   summarization at 800K is close, and it pays back on its first request.

## 4. Mark sets compared

Replayed with the shipped check: `chat_runner._cap_conductor_context` at
`ba317c8f61`, with each set's lines patched in and every hold as shipped.
Same turns, gaps and growth as R7, priced with the same fits on both sides.
A trimmed turn is cold and pays a 5.3 s start. The landing is built as
section 3.1 says.

| Marks | Trims per 100 turns | Context p50 / p90 | Cold first output p50 | Context credits per turn | Saved per day | Redo per day (proxy / bound) |
|---|---:|---|---|---|---:|---:|
| today | 0 | 508K / 728K | 15.8 s | 23.50 | 0 | 0 |
| 150/400 (the conductor's) | 6.7 | 122K / 242K | 6.4 s | 10.80 (−54%) | 2,503 | 8 / 30 |
| 250/600 | 4.5 | 156K / 388K | 7.1 s | 12.71 (−46%) | 2,128 | 10 / 36 |
| **Cold-only, Rule B 200K, safety 600K** | **6.1** | **132K / 368K** | **6.5 s** | **11.71 (−50%)** | **2,323** | **8 / 28** |
| Cold-only, Rule B 200K, safety 700K | 6.1 | 131K / 390K | 6.4 s | 11.98 (−49%) | 2,270 | 8 / 28 |
| Cold-only, Rule B 300K, safety 700K | 4.5 | 156K / 401K | 6.9 s | 12.97 (−45%) | 2,076 | 10 / 35 |

**Recommended: cold-only, Rule B over 200K after 300 s, safety line (Rule A)
at 600K.**

- It keeps 93% of the conductor marks' saving (2,323 against 2,503 credits a
  day, −50% of worker context credits), and cold first output falls from 15.8 s
  to 6.5 s at the p50.
- It trims on a warm cache 6 times in 4.5 days instead of 17, and only above
  600K.
- No turn crosses 800K inside a turn under it (40 today), so KAS's
  summarization is not reached. At 700K, one turn still crosses.
- Its redo is 0.65 credits and about 7 s per trim at the proxy (2.37 credits,
  30 s at the bound), against about 194 credits saved per trim.
- On the 1M window: workers keep using all of it inside a turn. Between turns,
  a trim keeps the item, the last report, the edited-file list and all the
  conversation text. It drops tool output and reasoning, which a worker
  re-reads from its worktree.

## 5. Design

### 5.1 Which sessions

A session is capped by the worker rules when all of these hold:

| Check | Source |
|---|---|
| its slot runs `kirocrew-worker` | the slot's agent |
| its session key has a work-ledger binding | `work_ledger.read_binding`: the same lookup `work_brief` uses, so only a dispatched worker, which has a brief to seed, is in scope |
| its backend is KAS | as in the conductor cap |
| the turn is a dashboard turn | `turn_admission.py`, the same call site as the conductor cap |
| the cap is on | `session.context_cap`, the existing switch |

Goal conductors keep their own marks and seed. Every other session is
unchanged.

### 5.2 Marks and rules

| Name | Value | Why |
|---|---|---|
| Rule B line | 200K tokens | over it, once the cache has expired, a trim costs no cache: the next request writes a new one either way |
| Cache life | 300 s from the previous turn's end | as the conductor cap: it never calls a warm cache cold |
| Safety line (Rule A) | 600K tokens | above it a worker reaches KAS's 800K summarization within a long turn; a warm trim here pays back on its first request |
| Tail budget | 100K tokens | LOW 150K − 46K base − 4K head; it has never bound on this host (max worker text 34K tokens) |
| Damping | 50K past the first reading after a trim | kept, as in the conductor cap |

The tail is sized in **tokens**, with the estimator Kiro Crew already uses
(`decisions/points/model_route.prompt_tokens`: ASCII chars / 4, every other
char one token). The conductor's 270,000 chars landed near 187K on its mixed
Chinese and English text, 1.81 chars per token. Worker text on this host is
0.12% non-ASCII, so a char budget would misjudge it the other way.

### 5.3 The seed

Built where every cold start builds its history
(`_session_replay_history`), in this order, with no model call:

1. Kiro Crew's usual new-session context.
2. **A trim note.** The chat moved to a new session at this turn's start. Tool
   output, reasoning and older messages are not in context. They are in the
   chat's transcript: `search_chat_history` searches it, `get_chat_session`
   reads it, and the file is `~/.kiro/crew/sessions/dashboard_<slot>.jsonl`.
   Before acting, re-read the files you are editing from your worktree, and
   re-read your PR's state.
3. **The work brief**: title, acceptance and the conductor's decision
   (`work_ledger.read_work_brief`).
4. **The item's latest `work_report`**: status, summary and artifacts
   (worktree, branch, head SHA, PR).
5. **The files edited** since the session started or the last trim, as paths
   only, newest first, at most 30. p50 13, p90 23 files; about 530 tokens at
   the p90. Only 5 of the 33 redo files were on it, so it is a pointer to the
   worktree, not a replacement for re-reading.
6. **The text tail** (`build_session_replay`, today's row rules), under the
   100K-token budget.
7. The turn's own message.

A worker re-grounds from what it already owns. Its worktree and branch hold
the code, and its PR holds the checks and reviews. The seed names all three,
so the first thing a trimmed worker does is re-read them.

### 5.4 Holds

Every hold from the conductor cap's section 3.4 applies unchanged: a slash
command, a queued reset, a pending question card or tool approval, attached
sub-agents, a busy session (`skip_if_busy`), no live session or an unknown
reading, and the damping guard.

### 5.5 Building the trimmed session off the hot path: a follow-up

Once the cache has expired (300 s after the last request started) on a chat
over the Rule B line, the gateway could end the old session and start the new
one then, so the next turn pays no start. Cold trims would cost no time.

| Measured over 4.5 days | Value |
|---|---|
| Idle episodes over the Rule B line | 141 |
| Of them, no later turn in that session | 15 (11%) |
| Of them, the next turn arrived within 30 s of the cache expiring | 28 (20%) |
| Start saved | 48 cold trims × 5.3 s ≈ 56 s a day |

Risks:

- **A session for a chat that never speaks again.** 11% of episodes. Each holds
  one KAS process (about 109 MB) until the idle reap.
- **A race with an arriving turn.** PR-loop wakes land 300 s after the
  previous turn ended (R7: 300.2 s p50), so 20% of episodes see a turn within
  30 s of the build. The build must take the session lock and give way to the
  turn.
- **Holds read at the wrong moment.** A question card, a queued reset or a
  sub-agent can appear between the build and the next turn. The turn start
  must re-check every hold and throw the pre-built session away if one holds.

The gain is about a minute a day on this host, so this RFC leaves it out.

### 5.6 Unchanged

- The conductor cap's marks, seed and scope.
- KAS's thresholds and `/compact`.
- `session.autocompact_pct` and the turn-end compaction gate.
- The 80,000-char replay of every other cold start.

## 6. Growth inside one turn

The cap cannot act mid-turn: `discard_conversation(..., skip_if_busy=True)`
refuses while a turn holds the session, and invariant 1 forbids it.

| Turns that cross a line inside the turn | Today | Recommended marks |
|---|---:|---:|
| 400K | 50 of 887 | 30 |
| 800K | 40 of 887 | 0 |

A first turn runs 66 requests at the p50 and 149 at the p90 (R5 q4_inputs).
One turn on 10-15 ran 83 requests over 40 minutes and hit KAS's own
compaction. **KAS's own compaction covers that case**: it summarizes at 80% of
the window and truncates at 95%. With the 600K safety line, a turn has to grow
200K inside itself to reach it, and none did on this host.

Follow-ups, not in this RFC:

- Cap the size of one tool result the worker receives, such as a long log or a
  whole file.
- Ask the worker, in its prompt, to end a turn at a milestone, so the next
  turn start can check.
- A KAS-side setting to drop old tool output inside a session.

## 7. #18931

On 2026-10-12, bolichen97 ruled on
[#18931](https://github.com/kirodotdev/KiroCrew/issues/18931): on KAS, Crew
keeps the engine's own compaction thresholds and does not override them. The
conductor cap revisited that for goal conductors. This RFC extends the same
revisit to dispatched workers, on the same terms:

- The cap is gateway-side. It ends a KAS conversation at a turn start through
  the same `discard_conversation(replay=True)` a reset uses, and starts a new
  one.
- It never calls KAS's compaction and never changes KAS's thresholds. KAS
  keeps its own 80% summarize and 95% truncate as the backstop inside a turn
  (section 6).
- `session.autocompact_pct` still does not apply on KAS.

The worker data adds a fifth reason to the conductor RFC's four: a worker's
growth is 53% tool output, which the engine's summary would compress anyway,
while the cap drops it for free on an expired cache.

## 8. Measures for the implementation

**Tests, through the real turn admission path** (`turn_admission.py` →
`_cap_conductor_context`), each failing first:

- A bound worker on KAS over 200K after 300 s is trimmed, and its next prompt
  carries the note, brief, report, edited files and tail.
- The same worker over 200K on a warm cache is not trimmed.
- Over 600K, it is trimmed warm or cold.
- An unbound `kirocrew-worker` chat, and a normal chat, are untouched.
- A goal conductor keeps its 150/400 marks and its seed.
- Each hold leaves the session in place.
- The tail stops at its token budget, not its char budget.
- The off switch turns it off.

**Before/after on this host**, in the implementation PR's body: the replay of
section 4 run against the implementation's own code.

**After 7 days live**, it passes when:

- worker context at a turn's first request is 200K or less at the p50;
- there are 10 trims per 100 worker turns or fewer, and 1 warm trim in 5 or
  fewer;
- worker context credits per turn are at least 40% lower;
- KAS summarizes a worker zero times;
- and, read by hand, no trimmed worker repeats a step it already finished or
  loses its PR.

## 9. Risks

- **What a trim forgets.** Tool output and reasoning. The proxy prices the
  redo at 0.65 credits a trim, but a proxy cannot see a fact that was only in
  a tool result and is never looked for again. The hand check in section 8
  covers it.
- **A worker mid-task.** A trim lands between two turns of a task. The brief,
  the report and the edited-file list say where it was. Its worktree holds the
  code exactly as it was.
- **A lookup that asks first.** `search_chat_history` and `get_chat_session`
  may need approval on a slot that asks before tools run.
- **A wrong scope.** A worker chat's metadata can name another agent. Scoping
  on the work-ledger binding, not only the agent's name, keeps an unbound chat
  out.

## 10. Decisions needed and rollout

1. **Acceptance** by the user (iamwhatever). On acceptance the status becomes
   `accepted`, with the decision and its date recorded here.

Then one implementation pull request: the scope check and the worker marks
(5.1, 5.2), the worker seed (5.3), and the tests and before/after replay
(section 8). The off-path build (5.5) and the in-turn options (section 6) are
later pull requests, each with its own measure.
