---
title: Bounded-time restore for large sessions - tail window for fully read tabs, focus-first order, one walk for a slot's folds, and a CI fixture at the threshold
status: draft
author: kirocrew agent session, directed by chenmingwei23
created: 2026-11-12
last-audited: 2026-11-12
audited-at: 029b6db029
doc-pr: 35916
implementation-prs: []
tracking-issues: [35875]
supersedes: []
superseded-by: []
---

# RFC: Bounded-time restore for large sessions

Status: draft. This is part B of [#35875](https://github.com/kirodotdev/KiroCrew/issues/35875). Part A (readiness in the composer) is a separate document. Main-side references were read at `029b6db029`. The document changes no code.

## Summary

The issue's target: after a gateway restart, the focused session accepts a message within 5 s with a 200 MB transcript and 100+ open tabs.

Measured on current main, the open-tab restore is no longer the 103 s the issue reports. Main already builds most tabs as sidebar rows from a 100-row tail. It reads the whole transcript only for the newest 8 tabs (`EAGER_WINDOW_RESTORES`) plus every tab whose window cannot wait: a tab not of person origin (linked channel, app, remote, cron or workflow session), a crew member's tab, a crew worker tab bound to an open work item, a tab an armed auto-nudge loop drives, a tab with an untorn local turn or deferred notes, a tab whose tail holds no row, and every tab when the loop store cannot be read. On the measured host that set was 8 tabs of 130. What still costs time:

1. The fully read tabs still read their whole transcript. A 204 MB transcript costs 1.2 s warm and 3.1 s cold, in the restore, before `ready`.
2. The restore order is by transcript mtime, not by what the user is looking at, so the focused tab can be the 9th tab and wait.
3. Crew-log slot folds. A cold fold walks the slot's whole unit once PER FOLD: 18 folds over a 136 MB unit took 91 s and read 8.4 GB. A stale savepoint (half the unit behind) still took 47 s (42 s with #35840).

This RFC proposes four changes. Section 1.3 is pure performance with identical outputs, and section 1.4 adds tests only. Sections 1.1 and 1.2 are behaviour changes, listed in section 3. Section 6 gives how they are delivered.

## Measurements

Scratch copy of a large home on one host, numbers only. Gateway on 127.0.0.1, current main `029b6db029`, 130 open tabs. The biggest transcript was built to 204 MB (40,156 rows) by repeating a real 68 MB one, and made the newest tab. "Cold" means every file under the scratch home was evicted from the page cache with `posix_fadvise(DONTNEED)` after `fsync`; `fincore` showed 0% resident before the run.

| Step | Warm | Cold | Where | Note |
|---|---|---|---|---|
| socket up | 3.4 s | 3.4 s | n/a | about 2 s of it is imports |
| restore start | 3.8 s | 3.9 s | `src/kiro_crew/dashboard/server.py` | awaits `_restore_dashboard_sessions` |
| open-tab restore, 130 tabs | 4.2 s | 10.1 s | `restore_open_slots_async` in `src/kiro_crew/dashboard/chat_persistence_parts/restore_drivers.py` | prefetch p50 14 / 23 ms, p95 36 / 214 ms, max 1.1 / 3.1 s |
| biggest tab full read (204 MB) | 1.2 s | 3.1 s | `_read_messages_locked` in `src/kiro_crew/history_projection_reads.py`, `message_row` in `src/kiro_crew/transcript_count.py` | JSON decode of every row is 60% of it |
| ready | 8.0 s | 14.1 s | `state.ready = True` in `src/kiro_crew/dashboard/server.py` | |
| memory preparation done | 9.4 s | 9.4 s | `src/kiro_crew/memory_startup.py` | overlaps the restore |
| 4,700 sessions on disk: `list_sessions` | 0.0 s | 3.1 s | `ConversationLog.list_sessions` in `src/kiro_crew/history.py` | restore start and ready unchanged by session count |

Crew-log slot folds, SYNTHETIC units (real crew-log units were not readable from the measuring environment). The units were written through the real `CrewLog` writer with made-up ids and text. Event mix per turn, about 10 KB: `message/received` (600 B text), `turn/started`, `request/configured`, `context/composed`, `step/started`, 12 `tool/called` + `tool/completed` pairs, `step/completed`, `message/sent` (3 KB text), `turn/completed`, and a `compaction/applied` every 50 turns. 18 slot folds were read through `fold_slot_warm`, one process per row, page cache evicted first. Real units with other mixes (more relays, larger fold states) may differ.

| Unit (synthetic) | Case | 18 folds wall | Bytes read | Per fold |
|---|---|---|---|---|
| 1.1 MB | cold | 1.0 s | 219 MB | 0.05 s |
| 22 MB | cold | 15.2 s | 1.5 GB | 0.9 s |
| 136 MB | cold | 90.9 s | 8.4 GB | 5.0 s |
| 136 MB | fresh savepoint | 0.17 s | 156 MB | 0.01 s |
| 136 MB | savepoint at 50%, main | 47.2 s | 1.4 GB | 2.6 s |
| 136 MB | savepoint at 50%, #35840 `2fe26df6c9` | 41.9 s | 1.4 GB | 2.3 s |

Hot path of a cold fold: `ProjectionRegistry.prime` (`src/kiro_crew/projection/registry.py`) -> the slot stream's `__iter__` (`src/kiro_crew/crew_log/projection_slot_memo.py`) -> `CrewLog._walk_from` (`src/kiro_crew/crew_log/store_handle.py`) -> `_iter_entries_located` (`src/kiro_crew/crew_log/store_segment_reads.py`) -> `Entry.from_dict` (`src/kiro_crew/crew_log/schema.py`) plus `json` decode. Every fold repeats that walk over the same unit. The bytes read are 3.4x the unit size per cold fold; the cause of the extra 2.4x was not pinned down.

## How the in-flight work interacts

- [#35206](https://github.com/kirodotdev/KiroCrew/pull/35206) and [#35496](https://github.com/kirodotdev/KiroCrew/pull/35496) (merged): a slot save appends or reuses rows. They make saving cheap, not loading; the restore read above is unchanged by them.
- [#34882](https://github.com/kirodotdev/KiroCrew/pull/34882) (merged): each unit replays from a watermark on boot. The fresh-savepoint row (0.17 s) is that path. This RFC keeps it as the steady state.
- [#35840](https://github.com/kirodotdev/KiroCrew/pull/35840) (open, for #35828): a resumed tail folds in place after one apply. Measured here as 11% off the stale-savepoint case for this event mix; its gain grows with fold-state size, which this mix keeps small. Section 1.3 composes with it: one walk feeds every fold's in-place step.
- [#35869](https://github.com/kirodotdev/KiroCrew/pull/35869) (open, for #35857): the worker list parses only changed item files. Not on the restore path measured here.
- [#34887](https://github.com/kirodotdev/KiroCrew/pull/34887) (open): refuses a send to a tab the restore has not built. Section 1.2 shortens how long that refusal can last for the focused tab.

## Goals

- The focused tab is built and accepts a message within 5 s of gateway start, cold cache, with a 200 MB transcript and 100+ open tabs.
- No restore step's cost grows with the full transcript size or the full crew-log unit size of a large session.
- A CI test fails when a change brings that growth back.

## Non-goals

- The composer readiness state and held send (part A).
- Keeping slot savepoints fresh (#33845).
- Session-listing cost (3.1 s cold for 4,700 sessions); it is not on the path to the focused tab today.

## 1. Design

### 1.1 Tail window for a fully read tab above the size threshold

Today the restore reads the whole chain with `read_messages_chained` for the newest 8 tabs (`EAGER_WINDOW_RESTORES`) plus every tab whose window cannot wait: a tab not of person origin (linked channel, app, remote, cron or workflow session), a crew member's tab, a crew worker tab bound to an open work item, a tab an armed auto-nudge loop drives, a tab with an untorn local turn or deferred notes, a tab whose tail holds no row, and every tab when the loop store cannot be read (`_may_restore_as_row`, and `_window_can_wait`, `_member_restore_identity` and `crew_bound_on_disk` in `_prefetch_open_slot_inputs`, all in `restore_drivers.py`). The tail window applies to every tab in that set, not only the newest 8. Above the threshold it instead reads the newest window with the existing `read_messages_chained_tail` (`history_projection_reads.py`), whose cost follows the row count and not the file size. The slot keeps a marker that older rows exist on disk. Scrolling up, or any reader that needs the full row list (export, fork, rewind, compaction, search in the tab), loads older rows from disk in pages through the same tail reader, off the loop.

Threshold: 16 MB of transcript. Cold, the full read costs about 15 ms per MB (3.1 s for 204 MB), so a tab at the threshold costs about 0.25 s, and a tab over it costs a window read instead (a 100-row tail of the 204 MB transcript read in 0.016 s warm; the 500-row window was not measured). Below 16 MB nothing changes.

The 5 s target does not follow from the threshold alone. The fully read set has no fixed size, so N tabs at the threshold cost N x 0.25 s cold: 8 tabs cost 2 s, and 3.9 s (cold restore start) + 2 s = 5.9 s, over the target. Lowering the threshold cannot fix that, because the set is unbounded; even 8 tabs would need about 9 MB (1.1 s / 8 / 15 ms per MB), and a host with more crew or channel tabs would miss again. So the target holds only together with section 1.2: the focused tab is built first, at about 3.9 s + 0.25 s = 4.2 s cold in the worst case at the threshold, or 3.9 s plus one window read over it, whatever the size of the rest of the set. The rest of the set is then bounded per tab (at most 0.25 s each), not per byte of history.

Window: the last 500 rows, or more if needed to reach the start of the current turn. The 100-row sidebar tail already serves the summary; 500 covers several screens and a resumed turn.

Pure perf for: the rows rendered at open, the turn the agent continues, the unread watermark. See section 3 for what a user can see.

### 1.2 Restore order by focus

The open-tab restore orders keys by transcript mtime (`_open_slot_restore_plan` in `restore_drivers.py`). New order: the tab focused at last shutdown first, then the tabs that were visible (split panes), then the rest by mtime. The dashboard already persists the open set to `open_slots.json`; it would also persist the focused key and visible keys. A client that connects during the restore and focuses a tab not yet built moves that tab to the front of the remaining queue.

With this order the focused tab is built in the first step of the restore, before the other 129, so the #34887 refusal for it ends at about restore start + its own read.

### 1.3 One walk for all of a slot's folds

A cold or stale-resumed slot fold walks the unit once per fold (18 walks above). Instead, when several folds of one slot need the same range, read the range once and step every fold's registry over each entry in that single pass. Each fold keeps its own state, savepoint and watermark, so the values are identical; only the read and the `Entry` build are shared. This composes with #35840's in-place step and with the backfill child (`slot_backfill_child.py`), which would fold all the slot's cold folds in one child instead of one each.

Expected effect from the numbers: in a profile of one cold fold the walk and parse are about 85% of its time, so 18 folds on one walk cost one walk plus 18 fold steps, roughly 17 s instead of 91 s for 136 MB, and roughly 10 s instead of 47 s for the stale half. Moving that one walk into the backfill child keeps it off the gateway's GIL.

### 1.4 CI fixture at the threshold

A generator, run inside the test's own data home, writes:

- one transcript at the 16 MB threshold (synthetic rows with the real row shape, made-up text), plus 40 small tabs, with `open_slots.json` naming them all and the large one focused;
- one crew-log session unit of about 16 MB with the event mix in the Measurements section, written through `CrewLog.append_many`.

The tests assert on counts, not on wall time, so they are stable on slow CI runners:

- the restore reads at most the window's bytes of the large transcript (count the bytes the tail reader returns, or wrap the file reads), and builds the focused tab first;
- a cold prime of all slot folds walks the unit once (count `_walk_from` calls), and the fold values equal the per-fold path's values for every registered fold.

One timing smoke test at the threshold may be added with a generous bound (for example 10 x the measured value), marked so it reports but does not fail the build on a slow runner.

## 2. Alternatives considered

- Raise or lower `EAGER_WINDOW_RESTORES`. Fewer eager tabs does not help when the focused tab is the large one; it is the read of that one tab that costs.
- Index each transcript by row offset. Faster paging, but a new on-disk format and a migration; the existing tail reader is enough for the threshold.
- Keep savepoints fresh so folds never go cold (#33845). Complementary; it does not help a first boot, a recreated unit or a stale savepoint after a crash, which is the case measured.

## 3. Behaviour changes

Everything not listed here is pure performance with identical outputs.

1. A large tab (over 16 MB) opens with its newest window loaded. Scrolling to the top loads older rows on demand, with a short loading row while they arrive. Today the whole history is in the window at open.
2. Restore order: the tab that was focused at shutdown, then visible tabs, are built first. Tabs appear in the sidebar in a different order during the restore; the final order is unchanged.
3. `open_slots.json` gains the focused key and visible keys. An older build reading it ignores the new fields.

## 4. Not verified

- Real crew-log units: real crew-log units were not readable from the measuring environment, so the fold rows are synthetic. Real units with large fold states (for example many work items) may be slower per entry and gain more from #35840.
- Whether any send to the focused tab waits on a slot fold today. Folds were timed offline; at boot they run in the background prime or backfill child, where they compete for the GIL (as #35828 observed), but no send path was traced to wait on one.
- The cause of the 3.4x bytes read per cold fold.
- Per-tab agent process spawn and the session search index, both unavailable in the measuring environment.

## 5. Decisions requested

1. Accept the 16 MB threshold and the 500-row window, or set others.
2. Accept the three behaviour changes in section 3.
3. Decided: section 1.3 is delivered as its own pull request (section 6).

## 6. Delivery plan

Each part lands as its own pull request. This document is the plan; none of them is in this pull request.

| Part | Kind | Pull request | Gate |
|---|---|---|---|
| 1.1 Tail window over 16 MB | behaviour change | separate, after review | review approval of this document first; no code before it |
| 1.2 Focus-first restore order | behaviour change | separate, after review | review approval of this document first; no code before it |
| 1.3 One walk per slot | pure performance, identical outputs | separate | fold values equal the per-fold path for every registered fold |
| 1.4 CI fixture at the threshold | tests only | separate | counts, not wall time |

## Appendix: line citations

The prose above names symbols, which survive a refactor. The lines below were read at `029b6db029` (main) for a reader who wants to jump to them.

```
src/kiro_crew/dashboard/server.py:1610
src/kiro_crew/dashboard/server.py:1681
src/kiro_crew/dashboard/chat_persistence_parts/restore_drivers.py:60
src/kiro_crew/dashboard/chat_persistence_parts/restore_drivers.py:264
src/kiro_crew/dashboard/chat_persistence_parts/restore_drivers.py:613
src/kiro_crew/history_projection_reads.py:333
src/kiro_crew/history_projection_reads.py:1100
src/kiro_crew/transcript_count.py:34
src/kiro_crew/history.py:697
src/kiro_crew/projection/registry.py:98
src/kiro_crew/crew_log/projection_slot_memo.py:368
src/kiro_crew/crew_log/store_handle.py:984
src/kiro_crew/crew_log/store_segment_reads.py:271
src/kiro_crew/crew_log/schema.py:570
```
