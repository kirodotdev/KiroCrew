---
title: Retire the Vector Memory card; add Audit, Test recall and decay weights
status: partial
author: buluoray
created: 2026-11-25
last-audited: 2026-11-26
audited-at: d8d692dbf1
doc-pr: 42890
implementation-prs: [43031, 42798]
tracking-issues: [42180, 42787]
supersedes: []
superseded-by: []
---

# RFC: Retire the Vector Memory card; add Audit, Test recall and decay weights

**Status:** partial. Accepted at team review, 2026-11-26 (maintainer Ray Xu). The document merged in
[#42890](https://github.com/kirodotdev/KiroCrew/pull/42890). On main: §7, the bot memory
tools `memory_list` and `propose_memory_changes` (one `crewmate.memory` card of at most 20 changes),
merged in [#42798](https://github.com/kirodotdev/KiroCrew/pull/42798) (`src/kiro_crew/mcp_mate.py`,
`src/kiro_crew/change_card_memory.py`). The new Memory page it
builds on merged in
[#43031](https://github.com/kirodotdev/KiroCrew/pull/43031), which squashes
the earlier drafts #42246 and #42276: its Graph tab (§8) draws the picked store
(`GET /api/memory/graph?store=`, `website/src/pages/overview/MemoryGraphTab.tsx`) and
`/settings/developer/memory` opens `/settings/memory?focus=graph`; the Embedding Model card shows
the setup progress, errors and Retry (`website/src/pages/overview/EmbeddingSetupStatus.tsx`).
The Vector Memory card still renders for its Audit and Inspector views
(`<VectorMemoryCard diagnosticsOnly />` in `website/src/pages/overview/MemoryTab.tsx`); the Audit
tab, Test recall and the decay weight are not built.
Every `file:line` below was read at `867ca8eab20` unless it names a branch.

The maintainer (@buluoray) made each decision below this week. This document
writes them down so a second maintainer can review them before the code lands.
It counts as large under CONTRIBUTING.md because it removes and replaces a
user-facing capability (the Vector Memory card).

## Summary

| # | Change | Kind |
|---|---|---|
| 1 | Remove the Vector Memory card. Move its download progress, errors and Retry to the Embedding Model card. | removed capability |
| 2 | Add an **Audit** tab on the Memory page. It has exact filters and counts, and it shows the source and both the before and after values. | replaced capability |
| 3 | Audit events stay append-only and are kept. Nothing prunes them. | decision; removes the unused `rotate_events` |
| 4 | **Test recall** replaces the Inspector. It shows what a bot gets at session start and with a given message. | replaced capability |
| 5 | Show a decay weight on each V1 experience, from the same function recall uses. | new display |
| 6 | Bot memory tools `memory_list` and `propose_memory_changes`, behind a card the user approves. | new capability |

## Problem

The Vector Memory card (`website/src/pages/overview/VectorMemoryCard.tsx`, 893
lines) sits on the global memory page under Memory Settings
(`MemoryTab`'s render). It has four tabs: Semantic, Episodic, Audit and
Inspector (its `view` switch).

1. **It cannot reach most facts.** The Semantic tab reads
   `/api/memory/semantic`, which caps `limit` at 1,000
   (`api_memory_semantic` in `dashboard/handlers/memory.py`). The tab filters only those rows. On
   Ray's store with 13,010 facts, 12,010 cannot be found there.
2. **It repeats the new Memory page.** The Memory page in #42246 lists facts,
   lessons and experiences with search, filters, sort and paging from the
   server. That covers the Semantic and Episodic tabs and the stat tiles.
3. **Its Audit tab hides data and miscounts.** `memory_events` stores
   `source`, `old_value` and `new_value` (`MEMORY_EVENTS_SQL` in `memory_schema.py`). The tab
   never shows `source`, and shows only one of the two values
   (the audit table's Details cell). Its type filter runs only on pages already
   loaded (`filteredEvents` and `eventTypes`), so filtered counts are wrong.
   `/api/memory/events` takes only `limit` and `offset`
   (`api_memory_events`).
4. **No rule says whether audit events are kept.** `rotate_events`
   (`vector_memory_runtime/store_bookkeeping.py`, cap `_MAX_EVENTS` = 10,000
   rows) would delete the oldest. Nothing outside tests calls it, so today every
   event stays, but only by accident.
5. **The Inspector does not show the real prompt.** It reads
   `/api/memory/context-preview` (`api_memory_context_preview`). On a V1
   store that route calls `get_semantic_context()` with no cap and then keeps
   lines that contain the query as a substring. The real session start reads
   memory through `read_session_memory`
   (`context_assembly/store_admission.py`), with caps, the
   preferences-only startup block and the relevance gate. The two can differ.
6. **Decay is invisible.** V1 recall scores an experience as
   `cosine * (0.7 + 0.3 * importance) * exp(-rate * days_old)`
   (`search_episodic`, `rank_from_scoring_set` and `episodic_candidate` in
   `episodic_search.py`; rate `_DEFAULT_DECAY_RATE` = 0.03/day, per tag via `memory.decay_rates`). A user cannot see
   why an old experience stopped coming back.

## Goals

- Nothing a user sees on the old card is lost. §1 maps every field.
- One place for each job: the Memory page lists records, Audit lists changes,
  Test recall shows the prompt, the Embedding Model card shows model state.
- Audit counts are exact over the whole store, not over loaded pages.
- Audit events are kept. Nothing in this RFC deletes one.

## Non-goals

- Pruning, capping or deleting audit events (§3).
- Changing what recall returns, how it ranks, or the decay formula.
- Adding decay to V2 stores.
- Changing the Memory page's records list (that is #42246 and #42276).
- Changing how the memory graph lays out or draws a store. §8 moves it, nothing
  more.
- Lessons in the bot memory card (follow-up named in #42787).

## 1. Every old field and where it goes

Old cites are `VectorMemoryCard.tsx` (VMC) on main. "Memory page" means the
page in #42246.

| Old item | VMC | New place |
|---|---|---|
| Title and info tip | 508 | Dropped: the page has its own title |
| Semantic, Episodic and Embedded counts | 597-599 | Memory page kind counts and the "N of M embedded" line |
| Embedding status badge | 604-626 | Embedding Model card header |
| Model label and path tooltip | 628-632, 173-191 | Embedding Model card |
| Setup progress: checking, downloading %, MB, verifying, retrying, ready, failed, bar | 566-590 | **Moves** to Embedding Model card |
| "Initializing… ~610MB", "Model loaded, engine starting" | 553-555 | **Moves** to Embedding Model card |
| Setup error, Retry (start embeddings), diagnostic details, legacy-vector warning | 510-548 | **Moves** to Embedding Model card |
| Stats read error and Retry | 510-534 | Embedded line error, with a Retry added |
| View switch | 636-650 | Memory page kind filter, plus the Audit tab and Test recall |
| Semantic: add key and value | 671-680 | Memory page "New memory" dialog |
| Semantic: key suggestions (allowlisted prefixes) | 323-337, 675 | **Moves** to the "New memory" dialog key field |
| Semantic: write error | 682 | Dialog error |
| Semantic: filter, Clear | 684-685 | Memory page search, over all rows |
| Semantic: Key, Value, Confidence badge, Source badge | 690-733, 437-442 | Memory page row and detail panel |
| Semantic: inline edit, delete with confirm | 704-734, 291-300 | Memory page Edit and Forget |
| Semantic: 100-row cap, "Showing x of y" | 139, 740-745 | Memory page paging (no 1,000 cap) |
| Semantic: `?lesson=` pinned row | 306-321, 702 | Memory page focus row (the link already lands there on #42246) |
| Episodic: search, tag chips, Text, Tags, Importance, Score, When, Delete, Load more | 757-812 | Memory page meaning search, tag facet, row fields, Forget, paging; **plus a decay weight (§5)** |
| Audit: event chips | 823-828 | Audit tab **Event** facet with exact counts |
| Audit: event badge colour (red refused, amber skipped, green other) | 840-842 | Red, amber and green on the Audit row, labelled in words. Red comes from the refusal code set (`SemanticRejectCode` plus `consolidation_value_size`), not the old substring match, which shows `low_confidence` or `work_state` in green |
| Audit: Key/Type | 843 | Audit row title, and a **Kind** facet |
| Audit: Details (new value, else old) | 844 | Audit detail shows **Before** and **After** |
| Audit: When | 845 | Row date and full time in detail |
| Audit: Load more, "Showing N events (M total)" | 850-853 | Footer "N of M events" and Load more |
| Audit: error, Retry, "No events" | 820-821, 838 | Same list states as the Memory page |
| — (stored, never shown) | — | Audit **Source** facet and detail field; raw `event_type` in detail |
| Inspector: query, Preview, Enter | 864-868 | Test recall "Task (optional)" and **Test recall** button |
| Inspector: empty query shows session start | 426-438 | Empty task runs and shows **At session start** |
| Inspector: semantic block | 871-876 | **At session start** box, as rows |
| Inspector: episodic block | 877-882 | **With this message** box, as rows |
| Inspector: raw text | 871-882 | "Exact text the bot gets" disclosure |
| Inspector: empty states, error, Retry | 861-862, 883-888 | "Nothing" per box; dialog error and Retry |
| `onActiveChange`, `onMigratedChange`, `onSettledChange` | 203-208, 357, 452-454 | Behaviour, not UI. `MemoryTab` uses them to gate the list. The page reads the same `vectorStats` and embedding status queries itself. |
| `onDirtyChange` (unsaved add or edit) | 456-457 | `MemoryTab`'s unsaved-changes guard reads the "New memory" dialog and row edit instead |
| `initialSemanticFilter`, `focusKey` | 203, 306-321 | The Memory page's `?focus=lessons` and `?lesson=` handling |
| `diagnosticsOnly` | 217, 256 | Only tests pass it; removed with the card |

## 2. Audit tab

![Audit tab mock: Event, Kind and Source facets, a Refused filter, and a detail panel with Before and After](assets/memory-audit-tab-mock.png)

_The mock's tab label still reads "History". The tab is named **Audit**._

- Place: a tab on the Memory page after Failures, for the global store and
  each bot store. `?store=` already selects the store
  (`_vector_tier_for_request`).
- Facets: **Event** (one label per `event_type`), **Kind** (Facts, Lessons,
  Experiences), **Source**. Each shows an exact count for the current filters.
- Kind comes from existing columns: `episodic` is Experiences; `semantic` with
  a key starting `lesson.` is Lessons; other `semantic` rows are Facts.
- Search matches key and text. Sort is newest or oldest.
- Detail: key, kind, source, time, raw `event_type`, Before, After, and a
  "Why" line for refusal codes only.
- Limit: experience writers keep only the first 200 characters of the text
  (the `create`, `restore` and `delete` writers in `episodic_write.py`). Before and After show what is stored.
- Unknown `event_type` values show raw.
- **V2 stores.** A V2 store writes no `create` or `update` event for a fact
  or lesson: `publish_committed_write` logs one only on V1
  (`vector_memory_runtime/semantic.py`). Its saves and changes are in
  `memory_revisions`. So on a V2 store the Audit tab lists both:
  `memory_events` for refusals, merges and deletes, and `memory_revisions`
  rows as Saved and Changed. Nothing new is written.

### API change

`GET /api/memory/events` gains filters and counts. Old calls keep working.

| Param | Today | New |
|---|---|---|
| `limit`, `offset` | yes (`limit` 1-200) | same |
| `event_type` | — | comma list |
| `kind` | — | `fact`, `lesson`, `experience` (comma list) |
| `source` | — | comma list |
| `q` | — | substring of key, old or new value |
| `order` | newest only | `desc` (default) or `asc` |

Response: `{events, total, facets: {event_type, kind, source}}`. `total` and
`facets` count the filtered rows over the whole table. The route keeps
`_redact_memory_field`. `q` matches the stored text, as
`/api/memory/semantic?q=` already does; both routes are owner-only. Counts use the existing
indexes on `(memory_type, created_at)` and `memory_key`
(`idx_events_type`, `idx_events_key`). If counting is slow on a large table, an index on
`event_type` is added in the same PR.

## 3. Audit events are kept

**Decision:** `memory_events` is append-only. Every event stays. Nothing
prunes, caps or deletes audit events.

Why: the table is the record of how each memory changed: what was saved,
changed, merged, refused or forgotten, and from which source (on V2 stores,
saves and changes are in `memory_revisions`, §2). For an experience it is the
only record of where the row came from (the comment on `MEMORY_EVENTS_SQL`). The
refusal counts on `/api/memory/observability` count the whole table
(`get_rejection_stats`), and they stay whole-history counts.

- `rotate_events` (`store_bookkeeping.py`) has no caller outside tests.
  P3 removes it and its tests, so no later change wires it up by accident.
  The same PR drops the facade alias in `vector_memory.py`, the entry in
  `test/test_vector_memory_composition_contract.py`, and the comments in
  `memory_schema.py` that name it or call `memory_events` a table the engine
  hard-deletes from.
- Forgetting a memory writes a `delete` event; it does not remove earlier
  events for that memory.
- The table grows with use. Open question 1 asks what to do about that later.
  Any answer must keep every event (for example, compressing old ones), never
  delete them.

## 4. Test recall

![Test recall mock: an optional task, an At session start box with facts and lessons, a With this message box with experiences, and the exact text](assets/memory-test-recall-mock.png)

- Place: the existing Test recall dialog on bot stores, also turned on for the
  global store.
- **At session start**: what `read_session_memory` puts in the session-start
  block for this store, plus the lessons block. An empty task is allowed.
- **With this message**: what session start adds when the typed task is the
  first message. That is the activity block `get_activity_context`
  (`memory.py`) builds inside `read_session_memory`: projects, recent history,
  and facts and experiences ranked against the task. It is not a per-turn
  injection.
- Rows show the same fields as the Memory page (confidence, key, match, tags,
  date). "Exact text the bot gets" shows both blocks verbatim.

### API change

`/api/memory/context-preview` changes its source to `read_session_memory`, with
the caps the store's sessions use. Its response keeps `semantic_context` and
`episodic_context` and adds `session_start` and `with_message`, each with
`text` and `rows`. The V1 substring filter in `api_memory_context_preview`
goes. The dialog switches from `api.memoryRecall` to this route, and
`vectorContextPreview` (`website/src/api/client/memory.ts`) gains the
`store` param it lacks today. `/api/memory/recall` is unchanged; it answers
"what the recall tool returns", which is a different question.

## 5. Decay made visible

- A V1 experience row shows a **Weight**:
  `(0.7 + 0.3 * importance) * exp(-rate * days_old)`. That is recall's score
  with the match set to 1, so it says how much age and importance alone hold
  the row back.
- The detail panel shows the rate and its source (default, or the tag in
  `memory.decay_rates` that set it).
- One helper computes it. `episodic_candidate`, `search_episodic` and the
  vectorised `rank_from_scoring_set` (`episodic_search.py`) call
  the same helper, so the shown value and recall cannot drift.
- V2 stores (most bot stores; the global store is always V1) do not decay. `search_episodic_v2` ranks with
  `memory_v2.rank_score` and an opt-in `memory.recency_boost`, default `0.0`
  (`config/memory_sections.py`). V2 rows show no weight.
- `/api/memory/episodic` and the records list add `decay_weight` and
  `decay_rate` on V1 rows only.

## 6. What links to the old card

| Dependent | Where | Change |
|---|---|---|
| Mount and callbacks | `MemoryTab.tsx` (import and render) | Remove; the page reads the stats and embedding queries itself |
| `SEMANTIC_CARD_ID` scroll target | `MemoryTab.tsx` import | Remove; the `?lesson=` link uses the Memory page focus row |
| `semanticValueText` | `MemberMemoryPanel.tsx` import | Move the helper to a shared module first |
| Comment cites | `components/FileRenderers.tsx` and `utils/formatCost.ts` comments | Reword |
| Unit tests | `website/src/test/VectorMemoryCard*.test.tsx`, `pages/overview/VectorMemoryCard.retryFocus.test.tsx`, `OverviewMemoryTabCov80.test.tsx` and other `MemoryTab*` tests that mock the card | Delete, or move to the Embedding Model card, Audit and Test recall |
| Playwright `embeddings-stat-badge` | `website/playwright/memory-embedding-evidence.spec.ts`, the `configured-inactive` row of `docs/ci/e2e-gate.md` | Point at the Embedding Model card header |
| i18n | `pages.overview.vectorMemoryCard.*` in every locale | Delete keys with no reader; move kept strings to their new owner |
| Docs | `docs/guides/install.md`, "Memory or knowledge search returns nothing" | It names the **Vector Memory** section of `kirocrew doctor`, not the card. It stays; add one line naming the Embedding Model card. |

## 7. Bot memory tools (#42798)

No RFC covers this. `rfc-memory-hygiene.md` covers write-time screening, table
choice and a code-only sweep. `rfc-memory-injection.md` §7 covers Dreaming, an
offline pass whose proposals are reviewed on the Memory page through
`propose_conflict`. Neither gives a bot tools to list or change its own memory
on request.

The decision is recorded in issue
[#42787](https://github.com/kirodotdev/KiroCrew/issues/42787), and this
document does not re-decide it:

- `memory_list` on `kirocrew-mate` reads one page of the bot's own facts or
  experiences.
- `propose_memory_changes` raises one `crewmate.memory` change card with up to
  20 deletes and edits, each with a reason. Only ticked rows apply.
- Only a user-sent turn can raise it. The store is the bot's own, filled in by
  the gateway; the Global store and other bots' stores are refused.
- Apply goes through the Memory page's own bulk preview and apply routes, so it
  writes the same audit events and revisions. Those show in the Audit tab.
  Today `memory_edit._write` records every bulk edit as `user_explicit`, so a
  card-applied change and a manual edit look the same. The apply path must
  pass the card's source through `apply_edit` so the Source facet can tell
  them apart (open question 3).

The two review paths stay separate on purpose. Dreaming proposals wait on the
Memory page because no person asked for them. Bot proposals answer a request in
chat, so they appear in that chat.

## 8. The memory graph moves to the Memory page

Amended 2026-11-26. This section replaces the earlier non-goal that kept the
graph under Settings > Developer > Memory.

The memory graph becomes a Graph tab on the Memory page, after Memories, and
the Developer > Memory entry goes away. An old `/settings/developer/memory`
link opens `/settings/memory?focus=graph`.

Why:

1. One memory surface. Every place a person looks at what the bot remembers
   is the Memory page; a second view under Developer split it in two.
2. The graph shares the page's bot picker, so it draws the store being read
   (`GET /api/memory/graph?store=`), not always Global memory.
3. The graph hands records to the list: a click on a fact or lesson opens it
   in Memories, and a box drawn on the canvas lists exactly that set there.

Undo: put the Developer > Memory case back in `DeveloperPanel.tsx` and drop
the Graph tab. The graph then draws Global memory again, with no hand-off to
the list.

## Rollout

Each phase is one PR and can ship or stop alone.

| Phase | PR | Exit criteria |
|---|---|---|
| P1 | Memory page list (#42246, #42276) | Merged. Every Semantic and Episodic row is reachable, including past row 1,000. |
| P2 | Embedding Model card takes setup progress, errors, Retry; "New memory" gets key suggestions | A test renders each setup state on the Embedding Model card with the old card absent. |
| P3 | `/api/memory/events` filters, `total`, facets; Audit tab; remove `rotate_events` | A test with events past one page asserts exact facet counts and `total`. On a V2 store, a saved fact shows as Saved. `grep -rn rotate_events src/` finds nothing. |
| P4 | Test recall on the global store, reading `read_session_memory` | A test asserts the dialog's text equals the text a real session-start build gets for the same store and message. |
| P5 | Decay weight helper and display | A test asserts the shown weight equals the factor recall multiplies in, on both scoring paths. |
| P6 | Remove `VectorMemoryCard.tsx` and its tests, keys and links | Blocked on P2, P3 and P4. `grep VectorMemoryCard website/src` finds nothing. |
| — | Bot memory tools (#42798) | Independent of P1-P6. |

## Backward compatibility

- `/api/memory/events` and `/api/memory/context-preview` keep every field they
  return today; new params are optional.
- Old links to `?view=memory` still open the Memory page.
- No phase deletes or rewrites stored memory or audit data.

## Undo

- P2-P6 revert as plain PRs. None changes data on disk.

## Security considerations

- The events and preview routes keep their store access checks
  (`_vector_tier_for_request`) and `_redact_memory_field`.
- The new `source` field is already stored and redacted with the rest of the
  row; it adds no new data.
- Test recall reads the same memory a session would read. It writes nothing,
  but it embeds the task, so it runs on the embed pool like the route does now.

## Alternatives considered

| Option | Why not |
|---|---|
| Keep the card and raise the 1,000 cap | Two lists for the same rows; the card's filters still run in the browser |
| Put Audit and Inspector under Settings > Developer | "What does my bot see" and "why was this refused" are user questions |
| Call the tab "History" | The detail panel already has a per-record "History" (`memory_revisions`); two different things would share a name |
| Prune events by age or by the existing 10,000-row cap | Deletes the record of how memories changed, and an experience's only record of origin (§3) |
| Test recall on `/api/memory/recall` only | That answers what the recall tool returns, not what the prompt gets |

## Open questions

1. Later: the audit table grows with use and is never pruned. How fast is not
   measured. If it ever needs to be smaller, the answer must keep every event,
   for example by compressing old ones. It must not delete them.
2. Should Test recall also show the recall-tool result when a task is typed,
   as a third section?
3. Does the card source land in #42798 itself, or in a follow-up beside P3?
