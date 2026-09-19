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

- **`seq`.** This module's `seq` IS the crew log's entry number: the header is 0 and the first event is 1, on the wire exactly as in the file. An earlier revision subtracted one at the boundary so the first event read as 0; that made the wire number permanently disagree with the stored one for the benefit of clients that all ship in this same change, so it was removed rather than left for an external contributor to build on. `last_seq` is read off the newest event rather than counted from the list, because a damaged committed line is skipped on load and a counted cursor would then hand a subscriber a position that re-delivers an event it already folded.
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

Each envelope's `data` is redacted through the shared exfiltration-URL and credential chain on its way out, the same chain the sibling `/activity` route runs, because this response crosses the same network boundary and would otherwise leak what its sibling protects. The pass covers dict KEYS as well as values. Every writer in this tree names its keys with code-owned structural words, so a credential-shaped key should be unproducible — but that is a property of today's writers rather than of the boundary, and one nested writer keying a dict by operator-supplied text would leak straight past a value-only pass. Two keys whose redaction collides merge, which requires both to have carried a credential, so what is lost is a value already destroyed.

Two WebSocket frames, both `{type, data}` like every other broadcast and both classified owner-only in `ws_event_scope`:

| frame | data | client rule |
|---|---|---|
| `members_subscribed` | `{lastSeqs: {slug: seq}}`, sent once to a new owner socket right after the connect snapshot | drop held rows whose `seq` exceeds `lastSeq` for that slug — they rode state a restart's torn-tail repair rolled back |
| `member_projection` | `{slug, key, value, seq}` — a whole projected value, emitted only when a unit's view changed | higher `seq` wins; a replay or a stale frame is dropped without checking contiguity |

The two rules are deliberately different. A whole-value frame needs no gap detection because a stale frame is simply lost to a newer one; only a delta channel would need a contiguity check, and this transport carries none.

The baseline carries a KNOWN open window, recorded here rather than left for a reader to find. Reading `lastSeqs` offloads to a thread, because on a first dashboard connect it parses every uncached member log and would otherwise stall the serving loop. The socket is already registered for owner broadcasts by then, so a `member_projection` append landing in that window is delivered ahead of a baseline computed before it, and the prune rule then deletes the newer row with no correction until that slug next changes.

Sending the baseline before registration does not fix it: the connect snapshot is the first frame a socket receives by contract, and `test_chat_send_echo_scope` reads that frame and treats its arrival as proof the socket is registered for echoes, so a baseline ahead of it fails four backend shards plus the E2E lane. The remedy is therefore per-socket suppression rather than reordering: hold `member_projection` back from a socket that has not yet received its baseline, through the `client_allowed` predicate the fan-out already consults per socket, then serve the current value for any slug that changed while it waited.

## 7. Client

`website/src/state/memberProjectionStore.ts` holds `Map<slug, Map<key, {value, seq}>>` under those two rules; `seed()` applies the baseline through the same higher-seq-wins path and never truncates, so a live frame that raced ahead of the baseline keeps winning. `useMemberProjection(slug, key)` binds a component through `useSyncExternalStore`; `useMemberRosterViews(slugs)` gives the page one referentially stable map for derived values — the starred count and filter, search and sort — so a pushed frame moves the row, the chip and the filter together.

`MembersPage` reads the roster row, the drawer's configuration and recent activity, and the patrol verdict from projections. The patrol block has two sources with two roles: the live auto-nudge registry is presence (a loop it holds as active is active), while the `wake` projection is the durable record, so a stop the registry has forgotten still renders with its reason. The registry query remains only for detail fields the projection does not carry (interval, cycle counts, next wake) and no longer polls.

## 8. Migration

The first `ensure(slug, name)` for a member with no log creates the header and folds the legacy files into events, in order: the DM binding, the rules text, then every line of `activity.jsonl.1` and `activity.jsonl`. The legacy files are left in place. `api_members` reconciles the folded roster against the agents config on read, so a config edited by hand becomes one `member/config` event with the fields that differed.

A slug is LOSSY: `slug_for_name` says so in its own docstring, and `Review_Agent`
and `review-agent` both fold to `review-agent`. Colliding names are SUPPORTED, and
attribution survives them because every activity entry stores the exact name. What
cannot be shared is a whole-member PROJECTION: one log folds one member's roster,
activity, wake and driving state, so serving it on a second member's row renders
the first member's work as the second's. The log header carries the name the log
was created for, so the roster read compares it against the row's own name and,
where they differ, logs a warning naming the remedy and serves that row an empty
projection -- visibly blank rather than quietly wrong.

A header whose name IS the slug is exempt from that comparison, because it names
nobody. `ensure` writes the header only while the log is fresh, so a writer with no
name in hand -- the message path passes `None`, and `emit` turns that into `name or
slug` -- would otherwise decide what the log claims for life. On the fresh path
`ensure` resolves that placeholder against the roster and writes the exact name
instead, using it for the migration below too, whose rules and binding reads are
name-scoped. Resolution runs only when the supplied name IS the slug and only when
the log is being created, so a member's config is read once ever rather than on
every message.

A placeholder therefore survives only where resolution comes up empty: a member the
config does not carry at the moment their first event is written. Should that member
later appear in the roster under a name the slug does not equal, the comparison must
not read the placeholder as a second member -- a slug is a lossy fold, so it differs
from almost every real name, and blanking the member's own state over a value that
was never a name is the wrong answer. `logged_name` reports what the header holds,
and the roster read is where the placeholder is recognised.

The migration is resumable, per item. `ensure` returning early on `log.exists()`
meant a process that died between `create` and the end of the migration left that
member's bindings, rules and activity unmigrated on every later call, because
nothing deletes the legacy files and so their presence cannot say whether the pass
ran. It now runs on every `ensure`, and each of the three items is skipped once the
log carries that item's event -- so whatever a dead run got through stays done and
the rest is picked up next time. A completion-marker event would answer the same
question in one check and is deliberately not used: it would sit in every member's
log forever and shift the seq of every event after it, for a concern that ends with
the first successful pass.
