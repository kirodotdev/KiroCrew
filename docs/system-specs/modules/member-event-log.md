# Member Event Log

Owners: `kiro_crew.eventlog`, `kiro_crew.eventlog_hooks`,
`kiro_crew.dashboard.handlers.members`, `kiro_crew.dashboard.websocket_hub`,
`website/src/state/memberProjectionStore.ts`, `website/src/state/useMemberProjection.ts`,
`website/src/api/membersQuery.ts`

## 1. Purpose

`kiro_crew.eventlog` gives every crew member one append-only log and derives the Members page's state from it. Before this module the page assembled each roster row at request time from thirteen sources — the agents config, a binding file, a rules file, a rotated activity file, the in-memory slot table, the auto-nudge registry and several client caches — and refreshed by re-fetching the whole roster on a coarse `refresh` frame plus a 60-second patrol poll. None of those sources recorded *what changed*, and a stop that the auto-nudge registry had forgotten collapsed into "no patrol scheduled".

The log is the record; every view is a fold of it. A change appends one event,
the event folds into projections, and changed projected values are pushed to
connected dashboards as whole values. Projection-backed fields update without a
roster refetch. `GET /api/members` remains the cold-start baseline and a
finite-stale registry read, and the activity query remains the source of
loading/error state and its `capped` flag.

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

Discovering member logs through `MemberEventLogService.slugs()` reads only each
log's header line, so that directory scan follows the number of members rather
than the size of their logs. This is service-level log discovery; the full roster
request separately ensures and folds each configured row. A slug comes from the
header rather than the directory name, and only when it folds back to the directory
it was found in — the fold is not reversible, and a directory carrying another
unit's id must not be enumerated as that other unit.

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

`GET /api/members` rows carry `projections: {asOfSeq, values}` — the baseline.
This tree currently exposes no raw-envelope HTTP read for member logs;
`MemberEventLogService.history()` is an internal page API.

Raw envelopes therefore stay in-process. At the network boundaries,
`GET /api/members/{slug}/activity` emits an allowlisted record and redacts its
operator-supplied `project`, while roster baselines and WebSocket projection values
run through the shared recursive exfiltration-URL and credential chain. The
projection pass covers dict KEYS as well as values, so a future nested writer cannot
leak operator-supplied text through a key. Two keys whose redaction collides merge,
which requires both to have carried a credential, so what is lost is already-redacted
content.

Two WebSocket frames, both `{type, data}` like every other broadcast and both classified owner-only in `ws_event_scope`:

| frame | data | client rule |
|---|---|---|
| `members_subscribed` | `{lastSeqs: {slug: seq}}`, sent once to a new owner socket right after the connect snapshot | drop held rows whose `seq` exceeds `lastSeq`, plus cached slugs omitted from the readable baseline; refetch the roster if anything was dropped |
| `member_projection` | `{slug, key, value, seq}` — a whole projected value, emitted only when a unit's view changed | higher `seq` wins; a replay or a stale frame is dropped without checking contiguity |

The two rules are deliberately different. A whole-value frame needs no gap detection because a stale frame is simply lost to a newer one; only a delta channel would need a contiguity check, and this transport carries none.

The baseline closes a race that reading it would otherwise open. Reading `lastSeqs` offloads to a thread, because on a first dashboard connect it parses every uncached member log and would otherwise stall the serving loop. The socket is already registered for owner broadcasts by then, so a `member_projection` append landing in that window would be delivered ahead of a baseline computed before it, and the prune rule would delete the newer row with no correction until that slug next changes.

Sending the baseline before registration does not fix it: the connect snapshot is the first frame a socket receives by contract, and `test_chat_send_echo_scope` reads that frame and treats its arrival as proof the socket is registered for echoes, so a baseline ahead of it fails four backend shards plus the E2E lane. The remedy is per-socket suppression rather than reordering. The socket is marked pending before the offloaded read; `client_allowed`, the predicate the fan-out already consults per socket, refuses `member_projection` to a marked socket and records the slug; once the baseline is sent the mark is cleared and each recorded slug's current projection is replayed. The mark is released on the read's failure path too, because a socket left marked would be suppressed for the rest of its life. The replay goes through the service's `redacted_snapshot`, so it runs the same network-boundary redaction as the broadcast instead of becoming a second egress path, and whole-value frames plus higher-seq-wins make replaying a value newer than the suppressed one harmless.

## 7. Client

`website/src/state/memberProjectionStore.ts` holds `Map<slug, Map<key, {value, seq}>>` under those two rules; `seed()` applies an ATTRIBUTABLE baseline through the same higher-seq-wins path and never truncates, so a live frame that raced ahead of the baseline keeps winning. An UNATTRIBUTABLE one is the exception, and the distinction is the sequence: `asOfSeq < 0` is not a position but the sentinel `api_members` emits when it will not attribute the slug — a shared slug, a header naming another member, or a failed read. Every real row's seq is above it, so comparing them would keep the whole cache, which for a collision is the OTHER member's projection. That block therefore clears the slug unconditionally and marks it refused, which also drops the live frames `apply()` would otherwise take: the roster is the only surface that checks attribution, while the publish and the on-connect replay both read a snapshot straight out of the service. The mark lifts on the next baseline carrying a real sequence. `useMemberProjection(slug, key)` binds a component through `useSyncExternalStore`; `useMemberRosterViews(slugs)` gives the page one referentially stable map for derived values — the starred count and filter, search and sort — so a pushed frame moves the row, the chip and the filter together.

`MembersPage` overlays projected roster fields on the `GET /api/members` query,
whose 30-second stale floor still provides cold-start and focus refresh. Recent
activity comes from the pushed projection when present, while the per-member
activity query remains for loading/error state, its `capped` flag, and older-gateway
fallback. The patrol block has two sources with two roles: the live auto-nudge
registry is presence (a loop it holds as active is active), while the `wake`
projection is the durable record, so a stop the registry has forgotten still
renders with its reason. The registry query remains only for detail fields the
projection does not carry (interval, cycle counts, next wake) and no longer polls.

## 8. Migration

**A folded activity row is MARKED as unverified.** The log is fenced, ordered and
append-only, and a reader is entitled to treat what is in it as having been written
through those guarantees. Legacy rows were not: `activity.jsonl` is agent-writable
and is read during `ensure` until the fenced completion marker exists, so whoever
can write it before that completed fold chooses what the fold imports. Each imported
row therefore carries
`legacy_unverified: true`, which records that its provenance is the file rather than
this log. The rows are not dropped -- they are that member's real history as far as
anything can tell, and discarding them would lose activity the dashboard has always
shown. `_activity_key` strips the marker before hashing, so the marker changes no
row's migration identity: a row a pass before this rule appended bare still dedupes
against the marked one a later pass writes, and no member gets a duplicate for having
been migrated by the older code.

**The legacy file is decoded with replacement, not strictly.** The fold already skips
a line it cannot parse as JSON, but that guard sits on the parse, and a strict decode
raises from the READ instead -- one frame further out, where nothing catches it. One
malformed byte in an agent-writable file would then propagate out of the migration
and make every later activity write fail, losing those records permanently.
Replacement turns the byte into U+FFFD, which makes the line invalid JSON and sends
it down the skip path that already exists.

**A projection frame whose redaction fails is DROPPED, never published raw.** The
redaction on the WebSocket egress exists because a folded view carries operator free
text -- an activity record's `project` can embed a credential or a presigned URL.
Falling back to the unredacted value would publish exactly what the redaction was
added to withhold, and would do it on the one input redaction could not handle, so
the failure mode would leak more reliably than the success path protects. A dropped
frame costs one projection update that the next change to that projection re-sends.

The first `ensure(slug, name)` for a member with no log creates the header. Every
`ensure` then resumes the legacy fold under the member unit's cross-process lease,
in order: the DM binding, the rules text, then every line of `activity.jsonl.1` and
`activity.jsonl`. Each item is deduplicated independently, so a crash can resume
without replaying completed work. Once the activity read completes, the service
durably creates `.legacy-activity-folded` inside the fenced unit directory before
retiring the source files by rename; the binding and rules sources remain because
their own events gate re-import. `api_members` reconciles the folded roster against
the agents config on read, so a hand edit becomes one `member/config` event with the
fields that differed.

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

The in-memory owner the activity scoping reads is taken from the loaded HEADER on
every `ensure`, not from that call's argument. The header is the authority because
it is written once, so on an existing log the argument is only whatever that writer
happened to hold, and a nameless writer holds the slug. Taking it would set the
owner to the slug and scope the member's own entries -- recorded under their real
name -- out of their own drawer. The read path answers the same way, so the write
and read paths cannot disagree about one slug, and the migration takes the header
name too because its rules and binding reads are scoped by that name.

The log has TWO ordinary writers: the gateway, and `kirocrew-core`, which runs as
its own subprocess and records member activity through the same service. The write
lease is taken non-blocking, so two writers arriving at the same instant do not
serialize behind the per-append lock -- the second is refused and writes nothing. An
append therefore WAITS OUT that refusal, briefly and boundedly, rather than losing
the event. Waiting works because the holder is momentary: the lease is released
within the append that took it, since the reload at the end of that call replaces
the handle the claim was bound to, and a test asserts this process holds no lease
after `ensure`, after two appends or after a read. Retrying is safe because the
store refuses before it writes, so no attempt can double-write, and exhausting the
budget re-raises so the callers' reporting still runs.

**A truncation drops the row AND asks for the baseline back.** When the server's
`lastSeqs` sit below a cached row, that row records something that did not happen and
is dropped -- but the truth is whatever the server holds at its own seq, and the
client-side store is a cache that cannot produce it. So the truncation answers whether
it dropped anything and the socket handler refetches the roster, whose rows carry each
slug's baseline. Seeding is higher-seq-wins, so the refetch cannot overwrite a newer
value that arrives meanwhile. A silent drop would render a blank card that reads
exactly like a member who has no such projection.

**The legacy activity file is folded in ONCE and then retired.** The fold dedupes by
counting matching rows, which cannot tell a row it has not reached from a row written
after it finished -- so counting alone would leave that agent-writable file a way to
enter the fenced ledger as trusted activity indefinitely. Completion is recorded by
the fenced `.legacy-activity-folded` sidecar, written and directory-synced only after
all rows are appended and before the live names are freed. The sources are then
renamed to `activity.jsonl.migrated[.1]` as hygiene. A crash before the marker leaves
the fold resumable; a crash after it cannot reopen the legacy name as a trusted input.
The read itself is streamed under a byte budget because it runs from `ensure`.

**This log has no rotation and accumulates over a member's lifetime.** Rotation
renames a file out from under its readers and drops its oldest rows, which a reader
folding by sequence cannot survive: the projection would silently lose rows it had
already folded. So the bounds here are per append and per value, and the growth bound
over a lifetime is the member's own activity rate. Stated rather than implied, because
the file-backed writer this replaced did rotate, and a reader who remembers that would
otherwise assume it still does. `MemberLog` nevertheless retains only the newest
`MAX_RETAINED_EVENTS` (5,000) envelopes in memory. `history()` pages older rows from
the crew-log store and projection priming streams the full history, so the memory
bound does not silently shorten either read.

**What that costs, measured.** The bounds above are per append and per value, so they
say nothing about the cost of reading a log that has grown, and it is the read that
grows. A changed file is reloaded WHOLE, and an append invalidates the cache and
reloads, so an append's cost tracks the log's own length. Measured on `MemberLog`
against a real log, median of fifty appends at each size:

| events in the log | one append | of which the parse |
|---|---|---|
| 0 | 3.0 ms | -- |
| 1,000 | 9.1 ms | |
| 2,000 | 15.1 ms | |
| 4,000 | 27.5 ms | |
| 8,000 | 52.0 ms | 50.1 ms |

So it is linear in the file, the parse is essentially all of it, and a doubling of the
log doubles every later append. For scale, a patrol waking every thirty minutes writes
about 17,500 events a year.

The reload is not where this can be fixed. The append needs the header before it can
write, so dropping the reload and invalidating only moves the same parse to the next
append: measured identical either way. Making an append cheap means reading the header
without parsing the events, which changes what this class promises and is tracked
separately rather than folded in here.

**The pending-append ceiling is RESERVED, not merely checked.** The future a caller
adds to the outstanding set does not exist until the pool accepts the work, so the
set cannot be added to while the decision is being made. A check alone therefore lets
several callers each pass a count that was true for all of them and then each add,
putting the set past the ceiling by one per caller. Reservations are counted
alongside the set and released the moment the future joins it, which makes the
decision and the claim one step.

**Queue acceptance is not a completed write, and only a caller that keeps a
RECORD of the write has to care.** `submit` answers whether the append was
queued; the append itself runs later on the ordered worker and can still fail
there. A caller that keeps no record loses exactly the event it handed over,
which the queue's ceiling already documents. A caller that keeps a CHECKPOINT
loses every later retry too, because its next pass compares against a checkpoint
claiming the event was written -- so the worker reports a failed append back and
the next pass recomputes exactly those transitions. The report carries a
CORRECTION rather than a key, because the two directions need opposite ones: a
failed open is retried by leaving the key absent from the checkpoint, while a
failed close has to put it BACK, since the checkpoint has already moved past that
key and removing it again computes nothing. That is the rule the slot
open/close path follows, and it is why the message and patrol paths correctly do
not read the answer.

Queued appends are drained before every HARD exit. `os._exit` skips `atexit`, so
the module's own hook does not run on the gateway's shutdown paths; both of them
drain explicitly, beside the sibling drain the session log already does there, and
a test asserts no hard exit in that module is left without one.

The drain waits for RESERVATIONS as well as registered futures. `submit` takes its
reservation, releases the lock to call `pool.submit`, and only then registers the
future, so for the length of that call an append is counted in the reservation count
and absent from the outstanding set. A drain that reads only the set sees nothing
there, answers that the log is complete, and the force-exit path that asked goes
straight to `os._exit` -- losing an append with no replay to recover it. There is
nothing to wait on inside that window, because the future does not exist yet, so a
reservation is polled at a short interval until it becomes one. The poll is bounded
by the drain's existing deadline rather than added to it, and a drain with nothing
outstanding still returns immediately.

A boot CLOSER is re-validated under the lock that writes it. The startup reconcile
decides each closer from a snapshot and appends it afterwards, and it runs as a
background task concurrent with the gateway going live -- so a slot it read as
durably open can legitimately be reopened, or a patrol re-armed, before the closer
lands. The log is append-only with no compaction, so a closer written after a live
open is a permanent regression of the projection, and not a self-correcting one: a
later restart's reconcile reads the state as closed and has no reason to reopen it.
The closer therefore goes through an append that re-asks whether the state it closes
is still there, handed the CURRENT projection rather than the caller's snapshot, and
writes nothing when it is not. Declining is a normal outcome, not a failure: it means
the state closed itself while the reconcile was deciding.

`emit` never PROPAGATES a failure, because a caller recording a transition must
not be brought down by its own bookkeeping, but it does not discard the outcome
either.
It answers whether the event landed, so a caller whose only record is this event can
tell an omitted transition from one that never happened, and it reports a failure
rather than logging it at debug, because that distinction is the one a projection
built from this log exists to make. The two boundary writers do not need the answer:
each fences its change through an authoritative store first and answers 500 when the
fence itself fails, so their event is a second copy rather than the record.

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
the rest is picked up next time. A completion-marker EVENT is deliberately not used:
it would sit in every member's sequence forever. The fenced sidecar file records only
the activity fold's one-time completion without shifting later event numbers.

**Only a regular file is read at the legacy path.** That path is agent-writable --
which is the whole reason the completion marker lives in the fenced log root -- so
the party the marker defends against also chooses what KIND of thing sits there. A
plain `open` trusts that choice: a FIFO blocks until a writer appears, and no writer
ever has to. The read runs inside `ensure` under the per-slug lock the roster request
path holds, and the hang happens BEFORE the marker can be written, so the FIFO
survives a restart and the marker never lands. The service therefore calls
`platform_compat.open_file_no_reparse(..., nonblocking=True)`: POSIX adds
`O_NONBLOCK` and `O_NOFOLLOW`, while Windows opens the reparse point itself with
`FILE_FLAG_OPEN_REPARSE_POINT`. An `fstat` then rejects anything that is not a
regular file. A non-regular entry contributes no rows; a genuine I/O failure keeps
the migration incomplete so a later `ensure` retries instead of retiring unread data.
