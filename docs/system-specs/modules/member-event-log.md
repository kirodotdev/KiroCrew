# Member Event Log

Owners: `kiro_crew.eventlog`, `kiro_crew.eventlog_hooks`, `kiro_crew.dashboard.handlers.members`, `website/src/state/memberProjectionStore.ts`

## 1. Purpose

`kiro_crew.eventlog` gives every crew member one append-only log and derives the Members page's state from it. Before this module the page assembled each roster row at request time from thirteen sources — the agents config, a binding file, a rules file, a rotated activity file, the in-memory slot table, the auto-nudge registry and several client caches — and refreshed by re-fetching the whole roster on a coarse `refresh` frame plus a 60-second patrol poll. None of those sources recorded *what changed*, and a stop that the auto-nudge registry had forgotten collapsed into "no patrol scheduled".

The log is the record; every view is a fold of it. A change appends one event, the event folds into projections, and the changed projected values are pushed to connected dashboards as whole values. The page renders from those values and re-fetches nothing.

## 2. Storage and the envelope

Each member's log is a `member`-kind **crew log**, so it lives at `<data_home>/crew-log/members/<store name>/log.jsonl`, where `<store name>` is the readable-plus-digest fold of the slug that every crew log uses. `kiro_crew.eventlog.log` is an adapter over `kiro_crew.crew_log.store`; the bytes, the locking, the durability, the torn-tail repair and retention all belong to that store, which already owns them for the `crew` and `session` kinds.

A third kind rather than a second mechanism, because the protection is named at the root: `crew-log` is masked from a sandboxed process (`sandbox._CREW_HIDDEN_LEAVES`) and refused to the agent's own file tools (`security.paths._CREW_SECRET_LEAVES`), so a kind placed under it inherits both. Dispatch trust reads this log, and an append-only record an agent can rewrite is not an append-only record — that property has to hold by where the file lives, not by someone remembering to add a second fence entry when a new log appears.

Line 1 is a header, not an event:

```
{"type":"member","version":1,"id":"<slug>","name":"<name>","createdAt":<epoch ms>}
```

Every later line is a crew log entry, which this module presents as the envelope `{"type", "seq", "time", "data"}`. Two translations live in the adapter and nowhere else, so nothing above it changes:

- **`seq`.** A crew log numbers the header 0 and the first entry 1. This module's `seq` is the zero-based position of the event *after* the header — the numbering its WebSocket frames and catch-up reads already speak — so the adapter subtracts one at the boundary rather than renumbering a protocol clients depend on.
- **Contributed types.** A crew log keeps one guest type namespace, `app:<name>/<action>`, and grants it to the `member` kind; the contribution protocol spells the same thing `<app>/<action>`. The stored form carries the `app:` prefix so the log's own ownership rule decides the write, and the emitter is derived from the type so a caller cannot attribute an entry to a different app. Reads give the protocol spelling back.

Damage is answered at three grains. A torn trailing line — trailing bytes that are not a complete line — is repaired by truncating to the last committed byte, and load then returns normally. A damaged line *inside* the committed region costs a reader that line and nothing else: refusing the whole file would turn one unreadable entry into a member whose entire history is unopenable, and the entry is unrecoverable either way. An unreadable **header** is fatal and raises `LogCorrupt`, because without it nothing in the file is attributable to this member; `members.record_activity` reports that as `False` and `members.read_activity` as `[]` rather than raising.

Writes are serialized per member in-process and across processes by the store's own lock, which re-reads committed state under the lock so a second writer takes the next `seq` rather than duplicating one.

Listing the roster reads only each log's header line, so listing cost follows the number of members, not the size of their logs. The slug comes from the header rather than the directory name, and only when it folds back to the directory it was found in — the fold is not reversible, and a directory carrying another unit's id must not be enumerated as that other unit.

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
| `members_subscribed` | `{lastSeqs: {slug: seq}}`, sent once to a new owner socket BEFORE that socket is registered for broadcasts | drop held BUILT-IN rows whose `seq` exceeds `lastSeq` for that slug — they rode state a restart's torn-tail repair rolled back |
| `member_projection` | `{slug, key, value, seq}` — a whole projected value, emitted only when a unit's view changed | higher `seq` wins; a replay or a stale frame is dropped without checking contiguity |

The baseline's ORDER is load-bearing, not cosmetic. It is a ceiling the client truncates against, so a `member_projection` that reaches the socket between the `lastSeqs` read and the baseline's send is applied and then dropped by the truncate behind it — the client loses live state it had already received. Registration is the only thing that makes a socket reachable by a broadcast, so the baseline is written first and the socket is registered after; that makes the ordering a fact instead of a hope.

Truncation covers built-in keys only. A contributed key's `seq` is the contributor's own fold position, a different domain from the member log's, so it is not comparable to `lastSeq`. Such a row is removed either by its own `value: null` teardown frame or, when that frame was missed because the browser was disconnected, by reconciling against the next `GET /api/members` — which lists exactly the contributed rows the gateway still holds.

The two rules are deliberately different. A whole-value frame needs no gap detection because a stale frame is simply lost to a newer one; only a delta channel would need a contiguity check, and this transport carries none.

## 7. Client

`website/src/state/memberProjectionStore.ts` holds `Map<slug, Map<key, {value, seq}>>` under those two rules; `seed()` applies the baseline through the same higher-seq-wins path and never truncates, so a live frame that raced ahead of the baseline keeps winning. `useMemberProjection(slug, key)` binds a component through `useSyncExternalStore`; `useMemberRosterViews(slugs)` gives the page one referentially stable map for derived values — the starred count and filter, search and sort — so a pushed frame moves the row, the chip and the filter together.

`MembersPage` reads the roster row, the drawer's configuration and recent activity, and the patrol verdict from projections. The patrol block has two sources with two roles: the live auto-nudge registry is presence (a loop it holds as active is active), while the `wake` projection is the durable record, so a stop the registry has forgotten still renders with its reason. The registry query remains only for detail fields the projection does not carry (interval, cycle counts, next wake) and no longer polls.

## 8. Migration

The first `ensure(slug, name)` for a member with no log creates the header and folds the legacy files into events, in order: the DM binding, the rules text, then every line of `activity.jsonl.1` and `activity.jsonl`. The legacy files are left in place.

The fold is RESUMABLE, because a crash between the header and the last legacy append leaves a log that exists and holds only part of the member's past. Existence alone therefore cannot mean "migrated": `ensure` on an existing log finishes an incomplete fold instead of returning. Each step is idempotent — a legacy record is appended only when the log does not already hold it — so resuming completes the work rather than duplicating it. Completion is remembered per PROCESS (a memo, so the common path costs two failed stats), never written as a durable marker: the log's own content stays the source of truth, which is what makes a gateway killed mid-fold self-healing instead of permanently short-circuited.

A member's ledger is keyed by its PERSISTED identity (`members.member_slug`, which prefers the agent record's `member_id`), never by the lossy `slug_for_name`. Slugification can fold two crew names onto one slug, which would mean one log and one projection block holding two members; and the roster read and the history route already key on the persisted identity, so a writer using the lossy form would also be writing where those readers never look. `eventlog_hooks.ledger_slug` is the one derivation every writer goes through.

`record_activity(..., dedupe_session=True)` asks the service whether the member's log already records
that `(member, session)` pair, and the answer covers the WHOLE log: the service holds those pairs as an
index, primed from the log on load and updated on each append. A scan of the most recent N events
instead answers "no" as soon as a busy member has pushed the original past N, and writes the duplicate
participation fact the caller asked it to suppress. `api_members` reconciles the folded roster against the agents config on read, so a config edited by hand becomes one `member/config` event with the fields that differed.
