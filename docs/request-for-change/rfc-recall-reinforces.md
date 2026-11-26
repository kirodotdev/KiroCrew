---
title: Recalls and edits refresh an experience's strength
status: draft
author: buluoray
created: 2026-11-26
last-audited: 2026-11-26
audited-at: af18ace5afb
doc-pr: 43081
implementation-prs: []
tracking-issues: [42180]
supersedes: []
superseded-by: []
---

# RFC: Recalls and edits refresh an experience's strength

**Status:** draft (proposed; awaiting team review). Nothing here is on main.
The maintainer (@buluoray) approved the design on 2026-11-26. It builds on the
strength view in [#43075](https://github.com/kirodotdev/KiroCrew/pull/43075)
and on a usage side table that counts recalls outside the store (the "used /
never used" table proposed with the memory index work,
[#42927](https://github.com/kirodotdev/KiroCrew/pull/42927)). Every
`file:line` was read at `af18ace5afb` (branch `crew/slack/memory-decay`), since
the strength view is not on main yet.

## Summary

| # | Change | Kind |
|---|---|---|
| 1 | A recall that puts an experience into a session's context makes it fade more slowly from then on, and starts its fade again from that day. | changed ranking |
| 2 | An edit to an experience starts its fade again from the edit day. | changed ranking |
| 3 | Recall counts live in a side table the gateway writes in the background, never on the recall path. | new derived table |
| 4 | Existing memories score exactly as today until they are next recalled or edited. | migration |

This is a V1 change. V2 recall does not decay (`memory_v2.rank_score`) and stays
as it is.

## 1. Problem

V1 recall scores an experience as
`cosine × (0.7 + 0.3 × importance) × exp(-rate × days_since_written)`
(`episodic_search.recall_score`). Only importance, the tag rate and age move it.
An experience that comes back in every session fades at the same speed as one
that never came back. At the default rate an importance-0.5 experience falls
under the fade line on day 27 (`memory_decay.days_to_line`), even if it was
used yesterday. The Memory page now says so ("Recalls and edits don't slow
fading"), which is true and is also the wrong behaviour for a memory.

Spaced-repetition systems (SM-2, FSRS) solve the same problem: each successful
review resets retrievability and raises stability, so a fact you keep using
stays available longer each time.

## 2. Formula

For one V1 experience:

- `b = 0.7 + 0.3 × importance` (unchanged).
- `S₀ = 1 / rate` days, from the tag rate as today (`decay_rule_for`). A rate of
  0 means `S₀ = ∞`: the row never fades, as today.
- `n` = counted recalls (section 3), read from the side table; 0 when the row
  has none.
- `S = min(S₀ × (1 + G)ⁿ, S_MAX)`, with `G = 0.5` and `S_MAX = 365` days.
- `t₀` = the latest of: written, last counted recall, last edit
  (`memory_record_meta.updated_at` at revision > 1).
- `strength(t) = b × exp(-(whole days from t₀ to t) / S)`.
- Recall score = `cosine × strength(now)`, so `recall_score` keeps its shape
  with `S` and `t₀` in place of `rate` and `created_at`.

With `n = 0` and no edit, `S = S₀` and `t₀ = written`, so the score is exactly
today's. Worked example at the default rate (`S₀` = 33.3 days), importance 0.5:

| Recalls | S (days) | Day it falls under 0.385 after its last recall |
|---|---|---|
| 0 | 33.3 | 27 |
| 1 | 50.0 | 40 |
| 2 | 75.0 | 60 |
| 4 | 168.8 | 134 |
| 6 or more | 365 (cap) | 290 |

`G` and `S_MAX` are module constants, measured and tuned in the build PR with
the recall-ranking harness, never config keys.

## 3. What counts as a recall

A recall counts when `get_episodic_context` puts the row into a session's
context: it passed the relevance gate and made the `limit` cut. At most one
count per row per UTC day. These do not count: Memory page searches, Test
recall, the graph, `search_episodic` called from the API or CLI, and a row that
ranked but was cut. That keeps the dashboard from training recall, and the
once-a-day rule keeps one long session from stacking counts.

## 4. The side table

`memory_usage(record_id TEXT PRIMARY KEY, recalls INTEGER, last_recalled_at TEXT)`
in the store's own database. It is derived: dropping it returns every row to
today's score, and nothing reads it as the truth about a memory.

- **Recall path.** After `get_episodic_context` picks its rows (at most
  `memory.episodic_max_results`, default 8), the gateway adds their ids to an
  in-process set keyed by `(store, day)`. No SQLite write.
- **Flush.** The existing maintenance loop writes the set in one transaction,
  one `INSERT ... ON CONFLICT DO UPDATE` per touched row, then patches the
  resident scoring set (`_EpisodicScoringSet`) in place.
- **Today's `last_accessed_at` touch** (`_touch_last_accessed`, debounced 60 s)
  stays as it is. It counts every search, so it is not used as a recall count
  and is not backfilled into the table.

## 5. What changes for existing memories

- **At ship:** nothing. No row has a usage entry, so every score is today's.
- **After ship:** a row that is recalled starts fading from its recall day and
  more slowly each time. A row that is never recalled fades exactly as today.
- **Edits:** an edit restarts the fade from the edit day without changing `S`.
  Edits before ship already set `updated_at`, so an experience edited before
  ship gets its fresh start on the first recompute. That is the one change that
  lands at ship, and only for edited rows (`revision > 1`). Open question 1 asks
  whether to count only edits after ship.
- **Memory page:** the curve becomes a line that jumps back up at each recall;
  the detail panel names the recall count and the last recall day. The line
  "Recalls and edits don't slow fading" goes away, and Save as lesson stays for
  a memory that should never fade.
- **Graph:** reads the same `recall_weight`, so its sizes and fades follow.

## 6. Cost per recall

| Step | Cost |
|---|---|
| Recall path | add at most 8 ids to an in-process set: a few microseconds, no I/O |
| Scoring | two more float arrays in the scoring set (`S`, `t₀`): 16 bytes per row, 1.6 MB at 100k rows; one more subtract and divide per row in the vectorised score |
| FAISS and stdlib paths | read `S` and `t₀` for each hit from the same in-memory map |
| Flush | one write transaction per maintenance tick, one upsert per row recalled since the last tick |
| Crash before a flush | the counts since the last tick are lost; recall is unaffected |

## 7. Risks

- **Rich get richer.** A row that is recalled ranks higher, so it is recalled
  more. The gate, the once-a-day rule and `S_MAX` bound it; the build PR
  measures it on the recall-ranking harness before shipping.
- **Ranking changes users notice.** Only for rows recalled after ship, and in
  the direction users expect.

## 8. Not a goal

- Changing V2 ranking or adding decay to V2.
- A setting for `G`, `S_MAX` or turning reinforcement off.
- Archiving or deleting rows nobody recalls (a separate retention change).

## 9. Open questions

1. Count edits made before ship, or only edits after it?
2. Should a recall by a bot's session reinforce a row in the global store, or
   only recalls from the store's own sessions?

## 10. Phases

| Phase | Change | Exit criteria |
|---|---|---|
| P1 | Side table, recall-path set, flush | A test recalls a row twice on one day and once the next: the table holds 2 counts. Dashboard search and Test recall add none. |
| P2 | `S` and `t₀` in all three scoring sites and `recall_weight` | With an empty table, every recall-ranking test passes unchanged. A test asserts the shown strength equals recall's score after a recall, on both scoring paths. |
| P3 | Memory page and graph | The detail panel shows the recall count and the jump in the curve; the old line is gone. |
