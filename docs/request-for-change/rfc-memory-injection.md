---
title: Put the right rules in the session-start memory block
status: draft
author: iamwhatever
created: 2026-11-09
last-audited: 2026-11-09
audited-at: 6a3d7011ca
doc-pr: null
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Put the right rules in the session-start memory block

- Status: draft
- Author: iamwhatever
- Created: 2026-11-09
- Measured at `6a3d7011ca`; code is cited by symbol and file. Store counts are from one developer host on
  2026-11-09: the default store and one private member store, opened read-only.
- Related: [rfc-memory-hygiene.md](rfc-memory-hygiene.md) (what consolidation
  writes into semantic memory and how a sweep keeps it small) and
  [rfc-consolidation-fresh-conversation.md](rfc-consolidation-fresh-conversation.md)
  (each consolidation job on an empty conversation). This RFC covers the lesson
  rows those writers produce and what the session-start block does with them.

## Summary

The session-start rule block (`[Learned corrections]`) is always full, and it is
full of the wrong rows. Its cap is 37,000 characters. The rows eligible for it
total about 185,000 characters in the default store and about 189,000 in the
private store, so truncation decides what the model sees. Truncation currently
picks rows by accident: with a greeting as the first message, the default store
showed 0 of its 95 user-taught rules.

This RFC proposes five steps, each behind a flag that defaults off, each run in
shadow first, none rewriting existing rows, each rolled back by turning its flag
off:

| Step | What | Where it acts |
|---|---|---|
| A | New consolidation lessons default to `on_topic`; `always` needs the user's own words | write time, new rows only |
| B | Private store: a new lesson at cosine >= 0.95 to a same-scope row updates that row | write time, new rows only |
| C | One relevance rule for every block: short words do not count as matches | render time |
| D | Fold near-duplicate rules at render time, with a visible "folded N" line | render time |
| E | Dream: an offline, gated, reversible cleanup that shrinks the rule pool toward the cap | offline, approval-gated |

Measurement reuses the existing injection receipts. No new shadow system.

One principle decides what is worth writing: a row is worth keeping only when it
is useful later **and** cannot be read back from another source (GitHub, git, a
work ledger). Kind routing enforce already applies this to facts. Step A applies
it to lessons, and step E applies it to the backlog.

## Motivation

### How the block is built today

| Block | Cap (symbol, file) | Relevance gate |
|---|---|---|
| Rule block | `_LESSONS_STARTUP_CAP = 37_000`, `src/kiro_crew/context_assembly/budget.py` | None. `directives = authored + untagged` (`src/kiro_crew/vector_memory_runtime/lessons.py`) is ranked and filled to the cap |
| Findings tier (`on_topic`) | `_LESSON_EXPERIENCE_CAP`, `budget.py` | `any_lesson_overlap` (`lessons.py`): any shared stemmed word admits the tier |
| Preferences | `_PREFS_STARTUP_CAP = 12_700`, `budget.py` | None |
| Task facts (V1) | `_SEMANTIC_MEMORY_CAP`, `budget.py` | Ranked by `get_semantic_context` (`src/kiro_crew/vector_memory_runtime/semantic.py`), no floor |
| Task facts (V2) | same | `semantic_candidates_v2` (`semantic.py`) requires `relevance_evidence` (`src/kiro_crew/memory_v2.py`), floors 0.62 / 0.57 (`memory_v2.py`) |
| Episodic | `_EPISODIC_INJECT_CAP = 3_000`, `budget.py` | Cosine floor |

The private member store reaches `get_lessons_context` without a query vector
(`src/kiro_crew/context_assembly/store_admission.py`), so its rule order is
always lexical. The default store passes one (`store_admission.py`).

### Measured numbers

Lesson rows by writer and tier, live rows only:

| Writer, tier | Default store | Private store |
|---|---|---|
| consolidation, `always` | 179 | 123 |
| consolidation, untagged | 218 | 291 |
| consolidation, `on_topic` | 261 | 65 |
| user-taught, `always` | 4 | 54 |
| user-taught, untagged | 95 | 1 |
| user-taught, `on_topic` | 3 | 16 |
| **rule-tier rows (`always` + untagged)** | **496, about 185k chars** | **469, about 189k chars** |

Other findings, from read-only replays on this host:

| Finding | Value |
|---|---|
| User-taught rules in the default rule block for a greeting | 0 of 95 |
| Rows the rule block keeps per session (82 sessions) | median 96 of 465 eligible |
| Findings that the one-letter word "a" overlaps | 304 |
| A two-letter greeting matching a task fact | matched a language-code value |
| Near-duplicates in the private store, leader-based fold at cosine >= 0.95 | about 77 rows |
| Same data, single-link clustering | one 27-row cluster whose weakest pair is 0.88, with 27 distinct texts |
| Injection receipt directory today | 10 files, 346,169 bytes, no row, byte or age cap |

### Why there are so many `always` rows

The consolidation model picks the tier, and nothing pushes back:

1. Unstated means standing. `LESSON_APPLIES_INSTRUCTION`
   (`src/kiro_crew/lesson_validation.py`) tells the model to omit the
   field when unsure, and an omitted field is served as a standing rule.
2. The prompt examples read like rules. The lessons key in
   `src/kiro_crew/history_consolidation_pass.py` gives `"always Y"` and
   `"never Z"` as its examples.
3. The model pays no budget cost. It never sees how full the 37k block is.
4. Each job may add up to `_MAX_LESSONS_PER_CONSOLIDATION = 10` lessons
   (`src/kiro_crew/vector_memory_constants.py`).

Hypothesis: many consolidation `always` rows come from orchestrator-to-worker
chats, where the "user" is another agent handing out process steps. We sampled
30 consolidation `always` rows (15 per store, fixed seed) and report only
aggregates:

| Store | Origin recorded | Origin was an agent-relayed session | Text is a process step (PR, CI, rebase, review, worker) |
|---|---|---|---|
| Private | 13 of 15 | 13 of 13 | 13 of 15 |
| Default | 0 of 15 | not measurable | 11 of 15 |

Verdict: **verified for the private store, not verifiable for the default
store.** Default-store consolidation rows do not record their source session.
"Agent-relayed" means the source transcript carries a relayed-message marker or
a worker brief call. The text column is a keyword heuristic.

### Write-time dedup today

| Store | Check | Scope |
|---|---|---|
| Default | cosine > 0.85 against existing lessons (`src/kiro_crew/vector_memory_runtime/lesson_write.py`), authority-aware | same scope |
| Private | key only: `_lesson_key(rule, scope)` (`src/kiro_crew/vector_memory_member.py`) | exact text and same scope |

## Goals

- The rule block holds standing rules a person actually set, not an accident of
  truncation order.
- New rows stop inflating the rule pool (A, B).
- Matching works the same way in every block and ignores noise words (C).
- Duplicates cost no room (D).
- The backlog shrinks toward the cap through reviewed, reversible changes (E).
- No user-facing regression: every step is measured in shadow against a control
  before enforce.

## Non-goals

- Lowering the 37k cap.
- A relevance floor on the rule block, lexical or cosine.
- Write dedup across scopes.
- Automatic deletion of any row.
- Single-link clustering, or dedup in the 0.85-0.90 band.
- A new shadow log beside injection receipts.

## Design

### A. Lesson tier on new consolidation writes

| Case | Today | Proposed |
|---|---|---|
| Consolidation, `applies` unstated | served as standing rule | stored `on_topic` |
| Consolidation, `applies: always` | standing rule | standing only with a `user_quote` field holding the user's own words from the transcript; otherwise stored `on_topic` |
| Consolidation, source turn relayed from another agent | same as above | same as above; an agent's process step is not a user quote |
| `learn_add`, `user_explicit` | unchanged | unchanged |

The quote is checked in code: it must be a substring of a user-authored turn in
the source conversation. The prompt examples change to show one finding and one
standing rule, and the instruction states that unstated now means `on_topic`.
The principle above goes into the lessons prompt: skip anything the model could
read back from GitHub, git or a ledger.

- Flag: `memory.lesson_tier_policy: off | shadow | enforce`, default `off`.
- Shadow: write as today; record in the receipt which new keys enforce would
  have stored `on_topic`.
- Existing rows: untouched. Old untagged rows keep being served as today.

### B. Private store write-time semantic dedup

A new consolidation lesson is compared with existing directives **in the same
scope and tier** using stored vectors. At cosine >= 0.95 the existing row is
updated (its revision goes through `memory_revisions`) instead of minting a new
key. A user-taught row is never overwritten by a consolidation write; the new
row is dropped instead and counted.

- Flag: `memory.member_lesson_dedup: off | shadow | enforce`, default `off`.
- Embed failure, missing vector or a vector-space mismatch: write as today.
- Scope isolation: unchanged.

The default store already merges at > 0.85 in the same scope. This RFC leaves
it alone. Measured 0.88 pairs in the private store were distinct rules, so the
private store should not drop to 0.85. Whether the default store should rise to
0.95 is an open question that shadow data from B answers.

### C. One relevance rule for every block

| Rule | Applies to |
|---|---|
| A word counts as an overlap only at >= 3 characters; a digit run counts at >= 3 digits; a CJK run of 2+ characters counts as one word | findings tier, task facts, every keyword half of a hybrid score |
| V1 task facts use the V2 gate: a row must carry `relevance_evidence` | `get_semantic_context` |
| History and projects inject as a short index; details come through `memory_recall` | activity blocks, default store |

The lexical rule already exists for the JSONL path
(`lesson_validation.py`); C makes `any_lesson_overlap` and V1 facts use it.

Why not a greeting gate. One option skips activity blocks when the first
message is only a greeting from a word list. This RFC rejects it:

1. A word list is per language and never complete ("ok", "继续", "status?").
2. The real bug is short tokens matching anything: "a" overlaps 304 findings
   and a two-letter greeting matched a language code. The word-length rule fixes
   greetings and every other short-token false match.
3. One general rule is easier to test and keep correct than a table.

- Flag: `memory.relevance_rule: off | shadow | enforce`, default `off`.

### D. Render-time fold of near-duplicate rules

At render time, before the budget fill, rules are folded with their stored
vectors:

- Order: user-taught first, then newest. Each row is compared with the
  **leaders already kept**, not with every row (no chaining).
- Fold when cosine >= 0.95 and scope and tier match.
- A user-taught row is never folded into another.
- Missing, zero, different-dimension or space-mismatched vectors never fold.
- The block gets one line: "folded N near-duplicate rules; learn_list shows
  all". Folds are not counted as budget omissions.

Cost: one matrix product over at most about 800 rows, measured near 5 ms.
D saves no characters, because the pool is larger than the cap; it frees room
for about 25 more distinct rules in the private store.

- Flag: `memory.lesson_fold: off | shadow | enforce`, default `off`.
- Before enforce: the golden test fixture that gives every topic-less row the
  same vector must give distinct vectors, or it folds everything.

### E. Dream: offline cleanup

A, B, C and D stop the pool from growing and use room better. They do not
shrink the backlog. Dream does. Its goal is to bring the rule pool (185k and
189k characters today) toward the 37k cap, so that truncation stops choosing by
accident. The cap does not change.

#### E.1 What others do

| System | What it does | What we take |
|---|---|---|
| Claude Code Auto Dream ([claudefa.st guide](https://claudefa.st/blog/guide/mechanics/auto-dream)) | Background pass. Runs only when both 24h have passed and more than 5 sessions happened since the last run. A lock file allows one run at a time. Writes only the memory directory. Four phases: orient, gather signal through narrow searches for corrections and explicit saves, merge and prune, re-index under the startup line limit. Unchanged files are left alone. One reported run took about 8-9 minutes over 913 sessions | Dual gate, lock, narrow candidate search, leave unchanged rows alone; avoid full rescans |
| Letta sleep-time compute ([blog](https://www.letta.com/blog/sleep-time-compute), [paper](https://arxiv.org/abs/2504.13171)) | A separate agent works on memory while the main agent is idle. Letta also names overgeneralization and memory rot as risks of repeated rewriting ([Letta](https://www.letta.com/blog/towards-agents-that-learn/)) | Run only when idle; treat model rewrites as a risk to measure, not a free win |
| Mem0 ([migration guide](https://docs.mem0.ai/migration/platform-v2-to-v3)) | Its new algorithm moved from extract-plus-merge with ADD/UPDATE/DELETE to a single ADD-only pass, and ranks current facts at retrieval time | Do not let a model update or delete at write time; keep A and B narrow and code-checked |
| SAGE ([arXiv 2605.30711](https://arxiv.org/abs/2605.30711)) | Code settles clearly novel facts as ADD and clearly redundant ones as NOOP; only uncertain cases reach an LLM merge, which cut add-phase cost | Code finds candidates; the model sees only the uncertain band |
| Nocturne sleep pass ([write-up](https://dev.to/harishkotra/sleeptime-memory-consolidation-an-agent-that-edits-its-own-memory-and-proves-it-still-remembers-53be)) | "Model proposes, code applies": each proposal is re-checked by a pure gate over store state (only given item ids, no invented facts); refusals are logged with a reason; snapshots before and after; rows deactivated, never deleted; a quiz after sleep checks that recall still works | Code gate on every proposal, logged refusals, tombstones, and a wake check after each run |

Content above was rephrased for compliance with licensing restrictions.

#### E.2 Trigger

All of these must hold:

1. At least 24h since the last completed run, **and** at least N new lesson
   rows or M sessions since then (defaults: 20 rows or 5 sessions).
2. No chat turn is running in any session.
3. A cross-process lock file under the data home is free. One run per host.

A manual trigger runs the same pass and still takes the lock and the idle
check. At most one automatic run a day.

#### E.3 Scope of one run

- Incremental from a watermark stored in `memory_meta`: a run reads only rows
  written or updated since the last watermark, plus the existing rows they are
  compared with.
- No full-store rescan per run. The one exception is a one-time backlog run,
  which is itself capped per night and resumable: it advances its own cursor and
  finishes over several nights.
- Per run: at most 200 rows considered and a wall-clock cap (default 10 min).
  The run stops at the next batch boundary when any chat starts.
- Embeddings for rows that lack one use the low-priority (bulk) embedding queue.

#### E.4 Candidates, found by code

| Candidate | How found | Model needed? |
|---|---|---|
| Exact copy | same normalized text, same scope | no |
| Near-duplicate | leader-based cosine >= 0.95, same scope and tier | no |
| Uncertain band | leader cosine 0.90-0.95, same scope and tier | yes, to write one merged sentence |
| Stale state | key and value patterns that kind routing already treats as one-off work state | no |
| PR-number families | many keys differing only by a PR or issue number | no; grouped for review |
| Legacy tier | consolidation `always` without a user quote, untagged consolidation rows | no; proposed as `on_topic` |

Only the uncertain band reaches the model: a few groups per run, under a token
cap (default 20k input tokens per run).

#### E.5 Model proposes, code gates

Every proposal passes a pure check over store state before it reaches the
approval list:

- Every cited id exists, is live, and was in this run's batch.
- A user-taught or pinned row is never changed automatically.
- A merged sentence adds no new facts: its content words come from the source
  rows.
- One row appears in at most one proposal per run.

A refused proposal is logged with its reason and counted in the run receipt.

#### E.6 Apply, approve, restore

- Output is an approval list, not a change. Owner rule: deletions need
  approval.
- An approved change goes through the existing edit and forget paths, which
  write `memory_revisions` and leave a tombstone, so every row restores
  (`src/kiro_crew/memory_edit.py` reads `memory_revisions` to restore a prior
  value).
- Trust may widen later for one narrow class (for example exact copies written
  by agents) only after a measured run of zero bad merges across a stated
  number of approvals. That widening is its own flag and its own decision.

#### E.7 Wake check

After an approved batch is applied:

1. Sample recent real startup queries from injection receipts. Receipts carry
   `session_key` and `user_msg_id`, so the query text is read from the local
   transcript at check time and never copied into the receipt.
2. Render the rule block and run recall for each query, before and after.
3. The run fails if any user-taught or pinned rule leaves the rule block, or if
   any recall hit is lost without a merged leader taking its place.
4. A failed run reverts its own changes through the same revisions.

#### E.8 Resource bounds

| Bound | Default |
|---|---|
| Runs per day | 1 automatic |
| Rows per run | 200 |
| Time per run | 10 min |
| Model input per run | 20k tokens |
| Embedding queue | bulk priority |
| Stop condition | a chat turn starts |

- Flag: `memory.dream: off | propose`, default `off`. `propose` only builds the
  approval list.

### Measurement: extend injection receipts

Injection receipts (`src/kiro_crew/injection_receipts.py`) already record, per
composition, the ids each lesson tier rendered. This RFC adds fields and caps:

- New fields: `would_drop`, `would_add`, `folded` (leader, folded) pairs, and a
  small `control` sample of rows the live policy omitted. Keys only, never text.
- New kinds must be added to `KINDS` (`injection_receipts.py`).
- Caps, copied from kind routing (`src/kiro_crew/memory_kind_routing.py`):
  150 rows a day, 2,000 rows total, 1 MB, 14-day sample window, counters kept
  400 days, reset on a policy-version change.

Go/no-go metrics, compared with the control sample:

| Miss signal | Meaning |
|---|---|
| A later `memory_recall` returns a row the policy dropped | the row was needed |
| A rule is re-taught with cosine > 0.85 to a dropped row | the user had to repeat it |
| A tool refusal matches a dropped rule | the model broke a rule it did not see |

A step goes to enforce only when its miss rate is no higher than control over
at least 14 days and 200 session starts.

### Optional follow-ups

Not core. Each needs its own decision:

- User-taught ordering inside the rule block (hash-key user rows ahead of
  consolidation `always`).
- A pinned guard set: a field only a person can set, its own small budget.

## Migration plan

| Phase | Ships | Exit criteria |
|---|---|---|
| 0 | Receipt fields and caps | receipts stay under every cap in a test that writes past them; shadow-off lines unchanged byte for byte |
| 1 | A and B in shadow | 14 days, 200 starts; counts of would-be `on_topic` and would-be merges recorded |
| 2 | C and D in shadow | same window; miss signals no higher than control |
| 3 | Dream in `propose` | approval lists produced; refusals logged; wake check passes on every applied batch |
| 4 | Enforce, one flag at a time | each flag's miss rate no higher than control |

Each phase is one pull request and can be abandoned alone.

## Backward compatibility

| Item | How it holds |
|---|---|
| Default vs private parity | Both use `get_lessons_context` (`store_admission.py`); C and D change both. A and B apply where each store writes. |
| Embed failure | D, B and E use stored vectors only; no query vector, no new timeout. Missing or mismatched vectors mean "no fold, write as today". |
| Untagged legacy rows | Still served as standing rules. Only Dream proposes a change, and only with approval. |
| Existing tests | Unchanged under `off` and `shadow`. Enforce changes are listed per step. |
| Rendering does not write the store | Receipts are held in memory at render time and written to JSONL after dispatch. |
| Compaction copy of the block | Uses the same renderer, so it follows the same flags. |

## How each step avoids user-facing regressions

| Step | Regression risk | Guard |
|---|---|---|
| A | a real standing rule filed `on_topic` | user-taught paths unchanged; a quoted rule stays `always`; shadow counts first |
| B | two different rules merged | 0.95 floor, same scope and tier, user-taught never overwritten |
| C | a needed fact stops matching | only noise words lose weight; shadow compares with control |
| D | a needed rule hidden | user-taught never folded; visible fold line; `learn_list` shows all |
| E | a row lost or merged badly | approval list, code gate, tombstones, wake check with auto-revert |

## Security considerations

- Dream writes only to the memory store, through existing edit and forget paths.
- No row leaves the host. Receipts hold keys, not text.
- The code gate refuses any id outside the batch, so a model output cannot
  touch rows it was not shown.
- The quote check in A blocks agent-relayed process steps from becoming
  standing rules.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Lower the 37k cap | With today's order, a lower cap keeps none of the default store's user-taught rules |
| Lexical floor on the rule block | Either keeps nearly all rows or drops safety rules |
| Cosine floor on the rule block | Real short queries do not separate related from unrelated rules |
| Exempt safety rules, then floor | The exempt set alone is larger than the cap |
| Greeting word list | Per language, never complete; C fixes the cause |
| Cross-scope write dedup | Breaks scope isolation and deletes without approval |
| Single-link clustering | Chains to 0.88 |
| Model-driven write-time update/delete | Mem0 moved away from it; harder to audit than an offline gated pass |

## Open questions

1. Should the default store's 0.85 write dedup rise to 0.95 to match B?
2. What zero-bad-merge record lets Dream apply exact copies without approval?
3. Dream trigger defaults: are 20 new rows or 5 sessions right for a light user?
