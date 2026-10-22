---
title: Keep semantic memory small enough for consolidation to dedupe against
status: draft
author: iamwhatever
created: 2026-10-22
last-audited: 2026-10-22
audited-at: e844065dea
doc-pr: null
implementation-prs: []
tracking-issues: [16354]
supersedes: []
superseded-by: []
---

# RFC: Keep semantic memory small enough for consolidation to dedupe against

- Status: draft
- Author: iamwhatever
- Created: 2026-10-22
- Tracks [#16354](https://github.com/kirodotdev/KiroCrew/issues/16354) and answers its "Direction to decide".
- Related: [#15659](https://github.com/kirodotdev/KiroCrew/pull/15659) (the 64 KiB cap and the delete fence), [#15537](https://github.com/kirodotdev/KiroCrew/issues/15537) (the report), [#15117](https://github.com/kirodotdev/KiroCrew/issues/15117) (fact revival), [#20448](https://github.com/kirodotdev/KiroCrew/issues/20448) (empty nightly summaries), [#20445](https://github.com/kirodotdev/KiroCrew/issues/20445) / [#20446](https://github.com/kirodotdev/KiroCrew/issues/20446) / [#20447](https://github.com/kirodotdev/KiroCrew/issues/20447) (lesson budget, receipts, lifecycle), [#9163](https://github.com/kirodotdev/KiroCrew/issues/9163) (authority order at lesson-dedup deletes), [#19830](https://github.com/kirodotdev/KiroCrew/issues/19830) (user-facing memory cleanup).
- Complements [rfc-consolidation-fresh-conversation.md](rfc-consolidation-fresh-conversation.md).

## Problem

Semantic memory only grows. Consolidation writes facts, and nothing removes them.
Once the store is past the prompt cap, the model cannot see most rows, so it cannot
update them and mints new keys instead.

The numbers below are from one developer host. Store counts are from
2026-10-20 unless marked 2026-10-22.

| Measure | Value |
|---|---|
| Live semantic rows | 9,953 (`project.*` 9,309, lessons 554, `pref.*` 48, `user.*` 42) |
| Live semantic rows, 2026-10-22 | 10,759 |
| `project.*` rows ever written / ever deleted | 9,422 / 113 |
| Semantic creates, 2026-09-11..10-02 (cap absent, most jobs failed) | 143 in total |
| Semantic creates, 2026-10-04..10-19 (after #15659) | 8,523 in total, 154-939 a day; 120 deletes |
| Rows the 64 KiB table shows | newest 361 of 9,953 (3.6%); 13 of 554 lessons |
| `project.*` rows that look like one-off work state (key or value heuristic) | about 65% |
| Rows repeating another row's exact value | 1,074 (827 of them short values like `true`, `0`, `6`) |

How it happens, at `e844065dea`:

1. `_bounded_semantic_table` (`history_consolidation_memory.py`) fits the table
   into `_SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION = 65_536` characters by keeping
   the newest `updated_at` rows.
2. The prompt (`history_consolidation_pass.py`) says "If a key already covers the
   same topic, UPDATE that key ... Do NOT create near-duplicate keys". The model can
   only do that for the 3.6% it sees.
3. A new key gets a fresh `updated_at`, so it stays visible and pushes older rows
   further down.
4. `_withhold_unseen_deletes` refuses a delete of a key the model never saw. That
   fence is correct and stays. It also means consolidation can remove neither copy
   once both are below the cut.
5. No scheduled sweep exists for semantic memory. `knowledge/dedup.py` is the
   Knowledge Base's document sweep only.
6. Nothing screens one-off work state at write time.
   `contains_volatile_lesson_fact` screens lessons for model identity and model
   pins, nothing else.

Most of what piles up is work state, not knowledge. A typical row looks like
`project.x.pr1234_status = "merged"`. It is true for a day and useless after.

Lessons are in better shape. `write_lesson` (`vector_memory_runtime/lesson_write.py`)
dedups at write time against every lesson in the scope, not just the visible ones:
substring containment, shared keywords of at least 50% of the larger set, or cosine
above 0.85. It is scope-local (`lesson_rows_in_scope`) and follows
`tier_permissions`. On 2026-10-22 the only exact-text duplicate is one global copy
plus one repo-scoped copy of the same rule. On 2026-10-20, 23 same-scope pairs had
keyword Jaccard of at least 0.25, and 14 of them fell below both the keyword-overlap
and the cosine threshold.

## How this fits the fresh-conversation RFC

[rfc-consolidation-fresh-conversation.md](rfc-consolidation-fresh-conversation.md)
stops jobs stacking inside one conversation. By its own numbers a job is about
80 KB, about 65 KB of it the semantic table. That is the 64 KiB cap, filled. It
leaves "fewer jobs" (its option E) as a follow-up.

This RFC does not change how many jobs run or how a conversation is reset. It
changes what a job writes (fewer one-off rows) and which rows fill the table
(the ones that matter to this span). Once the store fits under the cap again,
each job also carries less. Fewer jobs stays with #20448 and option E.

## Proposal

Four parts. Each ships behind its own flag and can land alone.

### 1. Screen one-off work state at write time

Before a consolidation semantic write, refuse a `project.*` item when both hold:

- the key names a work item: a PR, issue or CR number, a round, cycle or wave
  counter, or a SHA;
- the value is a bare state: a boolean, a number, a status word (`merged`,
  `open`, `green`, `pending`, ...), a SHA or a path.

The refusal uses a new `SemanticRejectCode` added to `_AUDITABLE_REJECT_CODES`,
so `log_reject_event` records it in `memory_events` like every other audited
reject. The prompt gains one line: events go in `episodic`, not in `semantic`.

It never applies to `source="user_explicit"`, to `pref.*` or `user.*`, or to
lessons. It runs on both write paths (`_write_structured_memory` and the member
store's `apply_consolidation`), placed next to `_withhold_unseen_deletes` so the
two cannot drift.

On 2026-10-22 this narrow rule matches 596 of 10,091 live `project.*` rows (6%).
The key test alone matches 4,812 (48%). The wider set is left to the sweep's
aged rule, because a work-item key can hold a durable decision.

### 2. Pick the table by relevance (the hybrid in #16354)

Answer to #16354: **hybrid**. Over the cap, fill the same 64 KiB budget in two
slices:

1. Rows nearest the span being consolidated, ranked by the existing V1 hybrid
   scorer (`semantic_candidates_v1`, which scores with `_hybrid_score`). The span
   text is the query. This is one embed per job.
2. The newest rows, as today, in the space left.

A default split of about three quarters relevance and one quarter recency is a
starting point to tune, not a fixed rule.

Recency-only cannot surface the original that a new row would duplicate.
Relevance-only would hide the rows most recently written, which are the likeliest
to be touched again. The hybrid keeps both.

What stays the same:

- The visible set is exactly the rows rendered. `_withhold_unseen_deletes`
  reads that set and is unchanged.
- `_SEMANTIC_OMISSION_NOTICE` still tells the model the table is partial.
- Rows render in key order, whole or not at all, as now.
- A store under the cap renders whole and byte-identical to today.

Lessons need no relevance slice for dedup, because `write_lesson` already checks
the whole scope. They keep their recency slice.

The spec paragraph on the bounded table in
`docs/system-specs/modules/memory-skills-hooks.md` changes in the same PR as the code.

### 3. A deterministic daily sweep, no LLM

Once a day, outside the consolidation queue, per store. Every fact rule reads
`project.*` rows only; `pref.*` and `user.*` are never swept, the same exemption
as part 1.

| Rule | What it retires |
|---|---|
| Same value in one family | rows in one `project.<name>.*` family with the same non-trivial value (24+ characters); the newest survives |
| Same stem | keys that differ only by a `_v2`, `_final`, `_new` or SHA suffix; the newest survives |
| Cross-scope identical lessons | a repo-scoped lesson whose rule equals a global one; proposed only (see below) |
| Aged work state | rows that match the part 1 screen and were not updated for N days (default 14) |

On 2026-10-22 the first three rules match 261 extra copies, 161 stem groups
(329 rows) and 1 lesson. The aged rule matches 220 rows. The 261 is far below the
1,074 in the Problem table because that count spans the whole store and every
value length, while this rule stays inside one family and skips values under 24
characters.

How it retires:

- **Supersede, never hard-delete.** It uses `delete_semantic` with
  `superseded_by` and `supersede_reason`, the path lesson dedup already uses.
  That stamps the record `superseded` and writes `memory_revisions` and
  `memory_events`. It passes `expect_value_json`, so a row that changed since the
  sweep read it is left alone.
- **Bounded.** At most a fixed number of rows per run (default 200).
- **Dry-run first.** The first runs only write a report: counts per rule and the
  keys they would retire. Applying is a separate setting.
- **Never retires `user_explicit` rows or user-saved lessons.** For those it
  writes an owner proposal instead, which the owner accepts or ignores.
- **Member (V2) stores keep owner review.** There a consolidation delete already
  becomes an owner proposal (`apply_consolidation` calls `propose_conflict`), so
  the sweep only proposes on a V2 store and never tombstones directly.
- Lessons are proposed, never retired, until #9163 settles authority order at
  lesson-dedup deletes.

"Not recalled for N days" would be a better aged rule than "not updated". Facts
have no recall stamp today: `last_accessed_at` is written only for episodic rows,
and the `semantic_memory` view does not expose it. Adding a
debounced stamp for facts, the way episodic does it, is the prerequisite for that
rule. Until then the aged rule reads `updated_at`.

### 4. Undo

- **See and restore one by one.** `/api/memory/retired` and
  `/api/memory/retired/restore` list and restore episodes only today. Extend both
  to facts the sweep superseded. Restore clears the tombstone in place, like the
  episode restore, so the row keeps its key, revision history and vector.
- **Revival stays a proposal.** At `e844065dea`, `_write_semantic` turns a
  non-user write to a tombstoned key into an owner proposal, not a silent revival
  (the #15117 class). The sweep keeps that path. Each such proposal for a
  sweep-retired key is counted, so a rule that retires live facts shows up.

## Cost

- Part 1: a regex check per semantic item. Nothing measurable.
- Part 2: one local embed of the span per job, plus the existing scorer over the
  rows. The table stays at or below 64 KiB.
- Part 3: one indexed scan per store per day, no model call.
- Consolidation today on this host: about 10-38 credits a day since 2026-10-03,
  under 1% of all usage, on a small model. A smaller store lowers it; nothing here
  raises it.

## Alternatives considered

| Option | What it is | Assessment |
|---|---|---|
| A | Screen + hybrid table + deterministic sweep (this RFC) | Chosen. Stops most of the growth at the source and lets the model dedupe against the rows that matter. |
| B | Raise the cap | Rejected. In September the prompt carried the whole table (about 560-640K characters) and most jobs failed. |
| C | An LLM cleanup pass | Rejected for now. It costs a model call over the whole store, and its deletes need the same fences anyway. |
| D | TTL only | Rejected. Age alone retires durable facts, and leaves duplicates that are new. |
| E | Do nothing | Rejected. The store gained 154-939 rows a day on this host, and the share the table can show keeps falling. |
| F | Recency-only, and say so in the spec | Rejected as the answer to #16354. It cannot surface the originals new rows duplicate. |

## Safety and undo

No existing fence gets weaker:

| Fence | Status |
|---|---|
| `_withhold_unseen_deletes` | Unchanged. It reads the rendered set, which part 2 still defines exactly. |
| `user_explicit` precedence in `_write_semantic` | Unchanged. Part 1 skips user writes; part 3 only proposes for user rows. |
| Scope-local lesson dedup (`lesson_rows_in_scope`) | Unchanged. Part 3 only proposes cross-scope matches. |
| `tier_permissions` | Unchanged. Part 3 retires no lessons. |
| Tombstone revival becomes a proposal | Unchanged, and now counted. |
| Owner review of automated deletes on member (V2) stores | Unchanged. The sweep only proposes there. |

Every retirement is a supersede with a revision and an event, restorable one row at
a time. Turning a flag off stops new retirements and leaves earlier ones restorable.

## Rollout

1. Part 1 behind a flag, on by default for new installs after one release of
   report-only logging.
2. Part 2 behind a flag. Compare duplicate creates per job with it on and off.
3. Part 3 in dry-run for at least a week. Read the reports, then enable apply with
   the per-run bound.
4. Part 4's fact restore lands before part 3 can apply.

## Measures

Read daily, per store:

- Live rows per family, and the one-off share.
- Duplicate groups (same value in one family, same stem).
- Share of rows the table can show, and table bytes per job.
- Rows created per day, and the 14-day rebound after a sweep.
- Revival proposals for sweep-retired keys.
- Consolidation credits per job.

## Questions for reviewers

1. Should a screened write be refused, or rewritten as an episodic event? Refusal
   is simpler; rewriting keeps the event.
2. Is 3/4 relevance and 1/4 recency a sound default split?
3. Should the aged rule wait for a fact recall stamp, or ship on `updated_at`?
4. Once the store fits under the cap, should the table budget drop below 64 KiB?
5. Should the sweep report surface in the dashboard as the start of #19830's
   user-facing cleanup?
