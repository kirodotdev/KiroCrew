---
title: Work fold forgets closed items — bound a board by what is open, not by what it ever created
status: draft
author: kirocrew agent session
created: 2026-10-10
last-audited: 2026-10-10
audited-at: bf1723d3f
doc-pr: null
implementation-prs: []
tracking-issues: [18583]
supersedes: []
superseded-by: []
---

# RFC: Work fold forgets closed items

Status: draft. Nothing built. Every code reference below was read at `bf1723d3f`.

This document lands on its own, before any implementation, because it removes a
user-visible limit (a conductor board stops dispatching after 256 lifetime items)
and changes what the board's projection holds. It amends
[`rfc-conductor-work-ledger.md`](rfc-conductor-work-ledger.md) in its own document,
so that one is not edited.

## 1. Problem

A conductor board refuses its 257th `create` for good, even when only one item on it
is still open. `_create_item` (`src/kiro_crew/work_ledger.py`) refuses with
`item_store_full` once `created_total >= MAX_STORED_ITEMS_PER_CONDUCTOR` (256,
`work_vocab.WORK_STORED_ITEM_LIMIT`). The counter only climbs; closing an item gives
nothing back. The only way out is `kirocrew ledger-sweep --purge`, run by a person,
and the sweep only removes a board whose items are all closed **and** idle for 30
days (`ledger_sweep.DEFAULT_OLDER_THAN_DAYS`). A long-lived queue board is never idle,
so it stays stuck.

Open fan-out is already bounded separately: `MAX_ITEMS_PER_CONDUCTOR = 32` counts only
open items and frees a slot on close. The 256 bound is the only one that never frees.

## 2. Why the bound exists, and why it counts closed items

The crew log itself is not the limit. It is an append-only log on disk, rolled into
units, read a unit at a time; it grows without a cap.

The limit is the `work` **fold**: the projection that replays the log into one
in-memory state per board. It keeps every item it has seen, closed ones included,
each with up to `WORK_EVENT_LIMIT = 200` event lines of up to 500 characters. The
fold-budget note in `crew_log/projection.py` measures the cap-state at **165.6 MiB**
for 256 items x 200 events. So the fold caps itself at `WORK_ITEM_LIMIT` (=
`WORK_STORED_ITEM_LIMIT`) and drops later creates into `omitted`, and the writer caps
lifetime creates at the same number so the fold never has to drop one
(`rebuild_from_projection` refuses a full fold with `omitted > 0`).

```
fold keeps closed items -> memory grows with lifetime creates -> fold caps at 256
-> writer caps lifetime creates at 256 -> board stuck after 256 dispatches
```

The root cause is the first arrow. A closed item does not need to live in the fold:
its full history stays in the crew log.

## 3. Proposal

The `work` fold **evicts an item when it closes** and keeps only:

- every open item (at most 32, enforced by the writer);
- a bounded window of the most recently closed items, for the surfaces that draw them
  (§5);
- a **tombstone set** of closed item ids, so late entries for them are recognised
  (§4.1);
- per-board **running counters** (created, accepted, rejected, abandoned) so totals
  stay answerable after eviction.

Replay stays correct. The log is in time order within the conductor's own units, and
`create` and `close` for one item both come from the conductor, so they interleave:
`create A, create B, close A, create C, close B ...`. During replay the fold holds the
items open at that moment, never the lifetime total. Peak size is bounded by open
fan-out, not by history.

With the fold bounded by open items, the 256 lifetime bound has no reason to exist.
The writer drops `created_total` as a ceiling (it may stay as a counter) and keeps
only `MAX_ITEMS_PER_CONDUCTOR`. A board can run indefinitely. Nothing is deleted from
the crew log; append-only is untouched.

## 4. Two hazards, checked against the code

### 4.1 Late entries for a closed item

The writer refuses every action on a terminal item (`CODE_ITEM_CLOSED` in
`work_ledger.py`; no reopen path exists in the writer). So no entry is **written**
after a close. But entries are **folded** per unit, not in global time: a worker's
`report` lives in the worker's own log, and the fold may reach that unit after the
conductor's unit that holds the `close`. The fold already handles the reverse order
with `parked` entries (`WORK_PARKED_LIMIT = 64`, for an entry seen before its
`create`).

After eviction, a report written before the close but folded after it would find no
item and be parked forever, filling the parked map and `omitted`.

Fix: before parking, check the tombstone set. An entry for a tombstoned id is dropped
and counted, not parked. Tombstones are ids only (tens of bytes), so thousands cost
little; the set is capped, and an id past the cap that later reappears falls back to
today's parked/omitted path.

Note: the fold today tolerates a reopen (`closed_at` cleared when an item returns to
`open`, `_workstreams_item_state`). The writer never emits one, so eviction treats
close as final. If a reopen action is ever added, it must rehydrate the item from the
log rather than from the fold.

### 4.2 Readers that use closed items

Readers of the `work` fold and of the writer's item cache, and what each needs:

| Reader | Uses closed items for | After this change |
|---|---|---|
| `crew_main_contract._work_fields` | `items_done` = accepted count | read the accepted counter |
| `_workstreams_render` (`workstreams` fold) | task rows, credits, timeline, drawer events | its own fold, already capped at `WORKSTREAMS_TASK_LIMIT = 40` per board; no change needed |
| `dashboard/card_lifecycle._bound_workers` | route a worker's log growth to its board | open items only is enough; a late report is dropped by §4.1 anyway |
| `dashboard/handlers/work_ledger_board.py`, `dashboard/handlers/work_ledger.py` | list a board's items, closed included | recent-closed window; older history marked as "in the crew log" |
| `probes/work_ledger.py` | count item files vs readable; any open item | unchanged semantics; counts shrink |
| `_create_item` backpressure | `list_work_items` open count | unchanged |

The writer's cache (`work-ledger/<slot>/items/`) must agree with the fold:
`rebuild_from_projection` already removes any item file the fold does not know. So
the cache keeps the same set the fold keeps. Closed records past the recent window
leave the cache. This removal runs in the server (rebuild, or the ledger-sweep's
existing per-store maintenance), never as a side effect of a model's `create`.

## 5. What changes

| File | Change |
|---|---|
| `crew_log/projection.py` | `work` fold: evict on close, recent-closed window, tombstone set, counters; tombstone check before parking; re-measure the fold budget note and its test |
| `work_vocab.py` | retire `WORK_STORED_ITEM_LIMIT` as a lifetime cap; add the window and tombstone caps |
| `work_ledger.py` | drop the `created_total` ceiling and its `item_store_full` branch; keep the open cap; rebuild keeps fold parity; trim closed records past the window under the conductor lock |
| `crew_main_contract.py` | `_work_fields` reads counters instead of counting items |
| `dashboard/handlers/work_ledger_board.py`, `dashboard/handlers/work_ledger.py`, board UI | show recent closed items, and say older history is in the crew log |
| `ledger_sweep.py` | unchanged purge rule; optional per-item trim of old closed records |
| `docs/system-specs/modules/session-work-ledger.md`, `src/kiro_crew/docs/work-ledger.md` | describe the open-bounded board |
| tests | replay with interleaved create/close past 256; late report after close; rebuild parity; counters |

## 6. Alternatives considered

- **Generation archive at the stored bound** ([#18584](https://github.com/kirodotdev/KiroCrew/pull/18584)).
  Keeps the 256 cap and, when a goal-free board is full and all closed, moves it aside
  and starts a fresh generation. Rejected: three review rounds showed the move cannot
  be atomic on Windows (the directory holds open lock handles, which is why
  `purge_conductor` removes contents instead of renaming), and a non-atomic move needs
  resume and rollback logic. It also still stalls a full board with one open item.
- **Automatic sweep with a "full and all closed" rule.** Simple, but still stalls a
  full board with one open item, and it deletes the whole board's history to make room.
- **Raise the number.** Pushes the wall out and grows the fold's worst case linearly.
  The board still stops one day.
- **Segment and checkpoint the work fold like the crew log.** Not needed: eviction
  bounds the fold by open items without changing how the fold replays.

## 7. Open questions

1. Size of the recent-closed window (proposal: 32, matching open fan-out) and of the
   tombstone cap.
2. Should the board UI fetch older closed items from the crew log on demand, or only
   say they exist?
3. Migration: a board already at 256 today. Proposal: the first rebuild under the new
   fold evicts its closed items and the board resumes; no data leaves the crew log.
