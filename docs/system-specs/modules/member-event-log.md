# Member Event Log

Owners: `kiro_crew.eventlog`, `kiro_crew.eventlog_hooks`, `kiro_crew.dashboard.handlers.members`, `website/src/state/memberProjectionStore.ts`

## 1. Purpose

`kiro_crew.eventlog` gives every crew member one append-only log and derives the Members page's state from it. Before this module the page assembled each roster row at request time from thirteen sources — the agents config, a binding file, a rules file, a rotated activity file, the in-memory slot table, the auto-nudge registry and several client caches — and refreshed by re-fetching the whole roster on a coarse `refresh` frame plus a 60-second patrol poll. None of those sources recorded *what changed*, and a stop that the auto-nudge registry had forgotten collapsed into "no patrol scheduled".

The log is the record; every view is a fold of it. A change appends one event, the event folds into projections, and the changed projected values are pushed to connected dashboards as whole values. The page renders from those values and re-fetches nothing.

## 2. Storage and the envelope

Each member owns `<data_home>/members/<slug>/log.jsonl`. Line 1 is a header, not an event:

```
{"type":"member","version":1,"id":"<slug>","name":"<name>","createdAt":<epoch ms>}
```

Every later line is an envelope `{"type", "seq", "time", "data"}`. `seq` is the zero-based position of the event after the header: `MemberLog.append()` assigns `len(events)` and refuses anything else, and `MemberLog.load()` treats a gap or a parse error inside the committed region as corruption (`LogCorrupt`). A torn trailing line — no newline, or unparsable — is repaired by truncating the file to the committed byte length and fsyncing; load then returns normally and the next append continues the sequence.

Writes are serialized per member. Each append opens the file in append mode, records the size, writes one line and fsyncs; on any failure it truncates back to the recorded size so a retry cannot produce a duplicate `seq`. The first write materialises the file through a temp file, a hard link and a directory fsync. The log is never rewritten or truncated for retention.

Listing the roster reads only each log's header line, so listing cost follows the number of members, not the size of their logs.

## 3. Event vocabulary

`kiro_crew.eventlog.types` is the closed vocabulary; `MemberLog.append()` rejects any other type.

| event | data | appended by |
|---|---|---|
| `member/config` | the roster's config-derived fields plus `changed: [field, ...]` | `handlers.agents` after a save that changed at least one roster field; `handlers.members.api_members` when the folded roster disagrees with the agents config (hand-edited config) |
| `member/binding` | `{slot_key}` | `handlers.members.api_member_thread` after the DM binding is written |
| `member/rules` | `{text}` | `handlers.members.api_member_rules_put` after the rules file is written |
| `member/message` | `{ts, preview}` | `DashboardState._broadcast_chat_message` for a member DM slot |
| `activity/record` | the participation record, including `ts` | `members.record_activity` (replaces the former `activity.jsonl`) |
| `slot/opened` · `slot/closed` | `{slot_key}` · `{slot_key, reason}` | the `slots` broadcast, diffing member-driven slots against the previous set |
| `patrol/started` · `patrol/stopped` | `{slot_key}` · `{slot_key, reason}` | the auto-nudge state callback in `slack.gateway` |

Live presence is deliberately not in the log. A slot's `running` flag and its approval prompts keep riding the `slots` frame; the log holds facts a person may later ask "when did that change, and why" about.

The binding and rules files remain. They are the trust subsystem's fail-closed fences, read on paths that never consult the log; the events beside them are the page's record of the same facts.

## 4. Projections

`kiro_crew.eventlog.projection.ProjectionRegistry` folds events through registered units. A unit is `{key, state_version, init(), apply(state, event), view(state)}`. `apply` returns the **same object** for an event it does not care about; the registry treats identity as "no change" and emits nothing for it. Folds are incremental — each new event passes through every unit once — and folded state is cached per `(key, slug)` with the `seq` it has observed. A unit sees a member's full history once, lazily, the first time that member is touched.

`kiro_crew.eventlog.members_projections` registers four units:

| key | view | fold |
|---|---|---|
| `roster` | the roster row minus `running`, with `name` and `slug` overlaid from the header | last-wins over `member/config`, `member/binding`, `member/message` |
| `activity` | `{recent: [record...] (≤50, newest first), today, week}` | ring buffer of `activity/record`; counts derived from each record's `ts` at view time |
| `wake` | `{patrol: armed \| stopped \| none, slot_key?, stopped_reason?, since?}` | `patrol/started` / `patrol/stopped` last-wins |
| `driving` | `{open: [slot_key...]}` | set add on `slot/opened`, remove on `slot/closed` |

`MemberEventLogService.snapshot(slug)` returns `{asOfSeq, values}` for all four. `history(slug, before, limit)` returns a newest-first page of envelopes.

## 5. Load-time closers

An open span whose owner is gone is closed by the reader, not by a bystander writing live. `eventlog_hooks.reconcile_members_at_startup()` runs once the auto-nudge service and the slot table have been restored: for every member whose `wake` says `armed` while the service holds no loop for that slot, it appends `patrol/stopped {reason: "interrupted"}`; for every `driving.open` slot absent from the slot table it appends `slot/closed {reason: "interrupted"}`. The closer is written, so the next reader does not recompute it, and a second run appends nothing. A patrol killed by a gateway restart therefore renders as "Patrol stopped — interrupted" instead of "no patrol scheduled".

## 6. Transport

`GET /api/members` rows carry `projections: {asOfSeq, values}` — the baseline. `GET /api/members/{slug}/history?before=&limit=` (limit 1–200, owner-gated like the activity route) pages the raw envelopes.

Two WebSocket frames, both `{type, data}` like every other broadcast and both classified owner-only in `ws_event_scope`:

| frame | data | client rule |
|---|---|---|
| `members_subscribed` | `{lastSeqs: {slug: seq}}`, sent once to a new owner socket right after the connect snapshot and before any later broadcast | drop held rows whose `seq` exceeds `lastSeq` for that slug — they rode state a restart's torn-tail repair rolled back |
| `member_projection` | `{slug, key, value, seq}` — a whole projected value, emitted only when a unit's view changed | higher `seq` wins; a replay or a stale frame is dropped without checking contiguity |

The two rules are deliberately different. A whole-value frame needs no gap detection because a stale frame is simply lost to a newer one; only a delta channel would need a contiguity check, and this transport carries none.

## 7. Client

`website/src/state/memberProjectionStore.ts` holds `Map<slug, Map<key, {value, seq}>>` under those two rules; `seed()` applies the baseline through the same higher-seq-wins path and never truncates, so a live frame that raced ahead of the baseline keeps winning. `useMemberProjection(slug, key)` binds a component through `useSyncExternalStore`; `useMemberRosterViews(slugs)` gives the page one referentially stable map for derived values — the starred count and filter, search and sort — so a pushed frame moves the row, the chip and the filter together.

`MembersPage` reads the roster row, the drawer's configuration and recent activity, and the patrol verdict from projections. The patrol block has two sources with two roles: the live auto-nudge registry is presence (a loop it holds as active is active), while the `wake` projection is the durable record, so a stop the registry has forgotten still renders with its reason. The registry query remains only for detail fields the projection does not carry (interval, cycle counts, next wake) and no longer polls.

## 8. Migration

The first `ensure(slug, name)` for a member with no log creates the header and folds the legacy files into events, in order: the DM binding, the rules text, then every line of `activity.jsonl.1` and `activity.jsonl`. The legacy files are left in place. `api_members` reconciles the folded roster against the agents config on read, so a config edited by hand becomes one `member/config` event with the fields that differed.
