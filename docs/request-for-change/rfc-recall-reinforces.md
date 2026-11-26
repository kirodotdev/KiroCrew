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
and on the usage side table `memory_usage(ident, last_used_at, uses)`, planned
as the fifth PR of the memory index series: recall use is counted in memory and
flushed in batches by the maintenance loop, in a table of its own, so recall
never writes the memory rows. The index RFC
([#42916](https://github.com/kirodotdev/KiroCrew/pull/42916)) lists usage
tracking as that separate change. Every
`file:line` was read at `af18ace5afb` (branch `crew/slack/memory-decay`), since
the strength view is not on main yet.

## Summary

| # | Change | Kind |
|---|---|---|
| 1 | A recall that relevance earned makes an experience's strength fade more slowly and starts its fade again from that day. Recall uses that strength only to settle near-ties: at most `MARGIN` (0.02) above today's score. | changed ranking |
| 2 | An edit to an experience starts its fade again from the edit day. | changed ranking |
| 3 | Recall counts live in the planned `memory_usage` table, which the gateway writes in the background, never on the recall path. | uses a planned derived table |
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
- `n` = counted recalls (section 3): `memory_usage.uses`; 0 when the row has
  no usage entry.
- `S = min(S₀ × (1 + G)ⁿ, S_MAX)`, with `G = 0.5` and `S_MAX = 4 × S₀`.
- `t₀` = the latest of: written, last counted recall, last edit
  (`memory_record_meta.updated_at` at revision > 1).
- `strength(t) = b × exp(-(whole days from t₀ to t) / S)`.
- `today(t)` = today's score, `cosine × b × exp(-(whole days from written to t) / S₀)`.
- Recall score = `min(cosine × strength(now), today(now) + MARGIN)`, with
  `MARGIN = 0.02`. Relevance decides the order; reinforcement can only reorder
  candidates whose scores are within 0.02 of each other today, and never lifts a
  row over one that is more relevant by more than that (section 7).
- The shown strength is that score at a cosine of 1, cap included:
  `recall_weight = min(strength(now), today(now) at cosine 1 + MARGIN)`. The page
  and the graph read it, so what they show is what recall uses.

So `strength(t)` is the memory's reinforced fade, and recall uses at most
`MARGIN` of it above today's score: reinforcement settles near-ties, it does
not keep an old memory on top. With `n = 0` and no edit, `S = S₀` and
`t₀ = written`, so the score is exactly today's.

Worked example of `strength(t)` (before the cap) at the default rate (`S₀` = 33.3
days, so `S_MAX` = 133.3 days), importance 0.5. It says how long a memory keeps
its tie-breaking edge; the recall score's lift is never more than `MARGIN`:

| Recalls | S (days) | Day `strength` falls under 0.385 after its last recall |
|---|---|---|
| 0 | 33.3 | 27 |
| 1 | 50.0 | 40 |
| 2 | 75.0 | 60 |
| 3 | 112.5 | 90 |
| 4 or more | 133.3 (cap) | 106 |

`G`, `S_MAX` and `MARGIN` are module constants, measured and tuned in the build
PR with the recall-ranking harness and the section 7 experiment, never config
keys.

## 3. What counts as a recall

A recall counts only when it was earned by relevance, not by strength:

- `get_episodic_context` put the row into a session's context (it passed the
  relevance gate and made the `limit` cut), **and**
- it passed the injection judge, where one runs, or its raw cosine cleared the
  admission floor (`episodic_relevance_threshold`) by at least 0.05. A row that
  made the cut only because strength ranked it first does not count.

At most one count per row per UTC day. These never count: Memory page searches,
Test recall, the graph, `search_episodic` called from the API or CLI, and a row
that ranked but was cut. That keeps the dashboard from training recall, and the
once-a-day rule keeps one long session from stacking counts.

## 4. The side table

This RFC uses the memory index series' usage table as planned,
`memory_usage(ident, last_used_at, uses)`, in the store's own database.
`ident` is the index's row identity (`('episodic', id)` on V1), `uses` is `n`
and `last_used_at` is the last counted recall. It is derived: dropping it
returns every row to today's score, and nothing reads it as the truth about a
memory. What this RFC adds is the rule for which uses count (section 3); the
table, its counter and its flush are that PR's.

- **Recall path.** After `get_episodic_context` picks its rows (at most
  `memory.episodic_max_results`, default 8), the gateway adds their idents to
  an in-process set keyed by `(store, day)`. No SQLite write.
- **Flush.** The existing maintenance loop writes the set in one transaction,
  one `INSERT ... ON CONFLICT DO UPDATE` per touched ident, then patches the
  resident scoring set (`_EpisodicScoringSet`) in place.
- **Today's `last_accessed_at` touch** (`_touch_last_accessed`, debounced 60 s)
  stays as it is. It counts every search, so it is not used as a recall count
  and is not backfilled into the table.

## 5. What changes for existing memories

- **At ship:** nothing. No row has a usage entry, so every score is today's.
- **After ship:** a row whose recalls relevance earned wins near-ties against
  rows that were never recalled, by at most `MARGIN`. A row that is never
  recalled scores exactly as today.
- **Edits:** an edit restarts the fade from the edit day without changing `S`.
  Edits before ship already set `updated_at`, so an experience edited before
  ship gets its fresh start on the first recompute. That is the one change that
  lands at ship, and only for edited rows (`revision > 1`). Open question 1 asks
  whether to count only edits after ship.
- **Memory page:** the curve shows `recall_weight`, cap included, so it rises by
  at most `MARGIN` at each recall;
  the detail panel names the recall count and the last recall day. The line
  "Recalls and edits don't slow fading" goes away, and Save as lesson stays for
  a memory that should never fade.
- **Graph:** reads the same `recall_weight`, so its sizes and fades follow.

## 6. Cost per recall

| Step | Cost |
|---|---|
| Recall path | add at most 8 ids to an in-process set: a few microseconds, no I/O |
| Scoring | two more float arrays in the scoring set (`S`, `t₀`): 16 bytes per row, 1.6 MB at 100k rows; per row in the vectorised score, one more subtract and divide, plus one exp and one min for the `MARGIN` cap |
| FAISS and stdlib paths | read `S` and `t₀` for each hit from the same in-memory map |
| Flush | one write transaction per maintenance tick, one upsert per row recalled since the last tick |
| Crash before a flush | the counts since the last tick are lost; recall is unaffected |

## 7. Feedback loop

xg-gh-25 flagged this risk in
[SwarmAI Discussion #79](https://github.com/xg-gh-25/SwarmAI/discussions/79)
("Recall Architecture — the READ path"): a hit log that drives decay can loop.
A memory injected because it is strong gets stronger because it was injected,
and over weeks a few memories hold every slot. Four guards, each already part
of the rules above:

1. **Count only earned recalls** (section 3): the judge passed it, or its
   cosine cleared the floor by 0.05. A row shown only because strength ranked it
   first does not count.
2. **Relevance first** (section 2): strength only breaks near-ties. The score is
   capped at today's score + `MARGIN` (0.02), so reinforcement never lifts a row
   over a candidate that is more relevant by more than that.
3. **Cap stability** at `S_MAX = 4 × S₀`, and count at most one recall per row
   per UTC day.
4. **Dashboard reads never count** (section 3).

**Acceptance check (rank concentration).** Before reinforcement ships, replay a
simulated month: the recall benchmark's situations, resampled to 30 days of
traffic at a measured host rate, through `get_episodic_context`, in three
settings: reinforcement off, reinforcement with no guard, and reinforcement with
guards 1-3. Measure every simulated day: the Gini coefficient and the top-10
share of counted recalls, the share of slots held by the top 1% of memories, and
gold recall on the test split at days 1, 15 and 30. **Pass:** with guards 1-3,
gold recall stays within 2 points of "reinforcement off", and the top-10 share
stays below 2× its day-1 value.

Ranking changes users notice: only for rows recalled after ship, and in the
direction users expect.

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
| P1 | The counting rule on `memory_usage` (needs the index series' usage PR) | A test recalls a row twice on one day and once the next: its `uses` is 2. Dashboard search and Test recall add none. |
| P2 | `S`, `t₀` and the `MARGIN` cap in all three scoring sites and `recall_weight` | With an empty table, every recall-ranking test passes unchanged. After a recall, a test asserts recall's score at an exact match equals the shown `recall_weight` (`min(strength, today + MARGIN)`), on both scoring paths, and that it is never more than `MARGIN` above today's score. The section 7 rank-concentration check passes. |
| P3 | Memory page and graph | The detail panel shows the recall count and the jump in the curve; the old line is gone. |
