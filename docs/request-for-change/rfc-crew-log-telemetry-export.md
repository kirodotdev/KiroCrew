---
title: Crew log based telemetry export -- one counting fold per module, behind the existing consent gate
status: draft
author: chenmingwei23
created: 2026-10-16
last-audited: 2026-10-16
audited-at: 1a2eb5435c
doc-pr: null
implementation-prs: []
tracking-issues: [21270, 21271]
supersedes: []
superseded-by: []
---

# RFC: crew log based telemetry export

- Status: draft. Nothing is built. Code facts are measured at `1a2eb5435c`.
- Author: chenmingwei23
- Tracking issues: [#21270](https://github.com/kirodotdev/KiroCrew/issues/21270)
  (the epic), [#21271](https://github.com/kirodotdev/KiroCrew/issues/21271) (this
  document)
- Related: [metrics.md](../system-specs/modules/metrics.md) (the instruments, the
  consent gate and the local sink that already exist),
  [crew-log-projection.md](../system-specs/modules/crew-log-projection.md) (the fold
  contract, the eager publish and the savepoints this design rests on),
  [crew-log-core.md](../system-specs/modules/crew-log-core.md),
  [crew-log-emitter.md](../system-specs/modules/crew-log-emitter.md)

## 1. Summary

Telemetry today cannot say how Kiro Crew is actually used. How many sessions a
conductor opens against a worker, how many turns each kind of actor causes, how
deep a session tree gets -- none of it is recorded as a metric. The raw facts are
already in the crew log: `session/opened` carries the agent name,
`turn/started` carries a closed `actor` value. What is missing is a summary layer
and one export path.

This RFC proposes both, in the smallest shape the existing code already supports:

1. **One small counting fold per module** on the crew log, each holding a fixed
   set of numbers and nothing else.
2. **One OpenTelemetry subscriber** on the projection bus, which reads those folds
   when they advance and records the numbers. It is a subscriber like any other,
   not a second path into the raw log.
3. **No new consent surface.** It is gated by `telemetry.enabled`, which is off by
   default and writes to a local sink with no network egress.

## 2. Motivation

`src/kiro_crew/metrics/` records the process and its machine: HTTP requests,
database calls, tool calls, turns, provider traffic, installed inventory. Those
instruments are keyed by the runtime, never by the shape of the crew running on
it, so a maintainer cannot answer a question about how the product is used from
them.

The crew log holds the facts, entry by entry. It just holds them as individual
events: answering "how many sessions did a worker open" from it means folding the
log, which is the thing a projection already does for every other reader.

## 3. Goals and non-goals

Goals:

- Report counts and distributions per module, derived from the crew log.
- Add no work to the crew log append path.
- Keep every attribute value inside a closed set that a reader can point at in the
  code.
- Ship behind the consent gate that already exists, still off by default.

Non-goals:

- Moving the existing instruments. Process, host, gateway internals and installed
  inventory stay in `src/kiro_crew/metrics/` and are not touched.
- A new egress path. No raw crew log entry is exported, and no new network
  destination is added.
- Per-person detail. See section 9.

## 4. Design

### 4.1 Which source owns which data

| Kind of data | Where it comes from |
|---|---|
| What happens inside a session, a crew or a member | a counting fold on the crew log |
| The process, the host, gateway internals, the installed inventory | the existing instruments in `src/kiro_crew/metrics/` |

The split is by kind of data, not by convenience. A fact the crew log already
records is read from the crew log; a fact only the running process knows stays
where it is measured.

### 4.2 The export goes through a projection

The crew log append path pays one `queue.Queue.put_nowait` and nothing else; the
eager worker folds the delta on another thread, and `crew_log/bus.py` publishes a
`FoldAdvanced` when it lands. The OpenTelemetry export subscribes to that
publish.

This is the whole reason the shape is a projection rather than a hook on the
append path: **what telemetry wants is counts and distributions, which are
summaries.** A projection is where a summary is computed in this codebase, and
putting it there means the append path carries no subscriber at all -- a slow or
failing exporter cannot reach a writer.

The opposite choice is reasonable for a different payload. A comparable system
that exports raw events forwards whole event bodies as log records: it has nothing
to summarise, so it hangs its forwarder off the event hook and sends
synchronously. When that same system does want a summary, it folds one first. The
shape follows the payload, not a preference, and our payload is a summary.

The export contract that S1 ([#21272](https://github.com/kirodotdev/KiroCrew/issues/21272))
establishes is this subscriber, kept deliberately small: two instrument kinds,
a counter and a histogram, and one subscription on `SCOPE_SESSION`
(`crew_log/bus.py`). It carries no observable-gauge channel and no loop over
scopes, because the one gauge this design needs does not ride the bus at all
(section 5). Each module's counters and histograms register only when the gate
is on.

### 4.3 One counting fold per module, not one export projection

The dashboard folds are the pattern to copy: `projection_dashboard.py` carries
`panel`, `agentic` and `mistakes` as three separate folds, each owning one subject
and each with its own limits.

A single export projection would instead run every module's step on every entry
any module names, and carry one `state_version` for all of them -- so a change to
one module's counters invalidates every saved copy of every other module's.
Separate folds keep that blast radius at one module.

A fold declares `affects`, `mode` and `state_version` in
`crew_log/projection_registry.py`. Eager is the default and requires `affects`,
and an eager fold must be named in `EAGER_SLOT_FOLD_NAMES` or
`EAGER_SESSION_FOLD_NAMES` or it is refused at import. A counting fold is
ordinary on all three counts.

### 4.4 What a counting fold may hold

A fixed set of numbers. The step adds one or subtracts one.

No lists, no identifiers, no names, no paths. That single rule is both the
privacy control and the performance control: `crew-log-projection.md` section 2
requires a fold's state to be bounded by construction, and a fold that only
counts is bounded by its own shape rather than by a limit someone has to
remember to set. Section 4.5's `agent_kind` mapping holds to this rule by doing
the classification in the fold's own `step`, the moment the raw values are seen,
rather than deferring it: both free-form strings it reads are locals inside that
call and neither is written to this fold's state, render or savepoint.

### 4.5 Every attribute value comes from a closed set

Each attribute value must point at a closed constant in the code, mapped down
in the fold -- not at the export layer -- to an enum with an `other` fallback.
The fold does the mapping so that a savepoint stores the already-mapped value
and a replay reproduces it from the log alone; nothing the mapping reads may
come from outside the log, or a restored count could disagree with the count
first recorded.

- `turn/started.actor` is already closed. `ACTOR_VALUES` in
  `crew_log/entry_types_lifecycle.py` declares eight values with
  `enum_closed=True`, and the emitter coerces anything outside the set to `other`
  before it builds the entry, so no call site can widen it. The attribute is the
  field as declared.
- `session/opened.agent` is **not** closed. It is declared a plain string
  (`Field("agent", JSON_STRING, required=True)`), because an operator's own agent
  spec is a legitimate value. S1 ([#21272](https://github.com/kirodotdev/KiroCrew/issues/21272))
  folds the opening entry in the `otel_usage` fold's own `step`, the moment
  `session/opened` lands, reading TWO of its fields because one kind of session
  states itself in each. The entry's own `slot` field is read first: a
  `member-<slug>` slot shape is a `crewmate` (below). The `agent` field answers
  the rest -- a filename in `OWNED_KIRO_AGENT_FILES` (`agent_files.py`) keeps its
  own value, the bare literal `kirocrew` (the emitter's own fallback agent name)
  maps to `default`, and everything else folds to `other`. The fold stores only
  the result
  (`opened_agent_kind`), never either raw string -- the classification function's
  imports of `agent_files` and `members` are function-local, so classifying a
  session costs nothing at module-import time and the crew-log package takes
  no module-scope dependency on either. The export subscriber reads
  `opened_agent_kind` already closed; its own closed-set check is a backstop
  against a damaged or stale render, not where the mapping happens.

A crew member's session is decided the same way, inside that same `step`,
without a new field. The fold's `agent_kind` mapping includes a `crewmate`
bucket, and the fact it reads is the opening entry's OWN `slot` field -- a
declared, required field on `session/opened` ("Slot key; may be empty" in
`crew_log/entry_types_lifecycle.py`) -- not the agent name and not crew
config: `members.is_member_session_key` checks whether that slot carries the
`member-<slug>` prefix (under either of its two travelling spellings,
`dashboard_member-<slug>` or `dashboard:member-<slug>`), a SHAPE check against
a string the log already wrote, never a lookup into who that member is or how
they are configured. It has to be the slot, because the slot key is the only
field that carries this fact: the members endpoint mints a crewmate's pinned DM
slot with the crewmate's own crew name as the `agent`, so that entry's `agent`
field holds a person-chosen name rather than anything the owned-file table could
recognise. Asking the agent name whether a session is a crewmate's can only
answer no, which is why the slot shape is tested first and the name table
answers the rest.

The slot value is treated exactly like the raw agent name: a local inside that
`step` call, read once to produce the enum and then dropped. It does not enter
this fold's state, render or savepoint, so a slot key -- which does carry a
member's slug -- is never stored or exported by this design. Two crews with
different member bindings fold the same slot-shaped key to `crewmate` the same
way, so a savepoint resumed under different bindings recounts identically.

The fold's savepoint holds only the enum result (`opened_agent_kind`) and the
other fixed numbers section 4.4 allows -- neither an operator-written agent
name nor the slot key the `crewmate` test reads ever reaches it. The export subscriber's own per-session memo (section 4.6) is a
separate piece of state, in memory, keyed on the session id for as long as
that session is tracked (section 4.7); it exists to turn a cumulative render
into a counter delta, not to classify anything, and section 9 states plainly
what it holds rather than claiming it holds no session identifier at all.

This is the bar `metrics/schema.py` already sets. It pins the `NS_CORE`
namespace, caps attribute count and value length, and states why: the recorder
caches one instrument per distinct name for the life of the process, so an
unbounded value is a cardinality bomb rather than a slightly noisy chart.

### 4.6 Consent, and where the numbers go

`TelemetryConfig` in `config/sections_telemetry.py` owns the gate.
`telemetry.enabled` defaults to false, and with it off the metric call sites are
no-ops that write nothing. With it on, aggregated metrics go to a local sink under
the data home and **no network egress happens**: `otlp_endpoint` is empty by
default and pointing it at a collector is a separate, deliberate opt-in. An
enterprise policy can pin the whole thing off through the
`capabilities.telemetry` governance scope.

This RFC adds no switch of its own. The subscriber is registered only when that
gate is on. A session's fold render is CUMULATIVE over the session's life, while
an OTel counter's `.add()` wants a DELTA, so the callback keeps an in-memory
per-session memo of the last rendered value it exported and diffs the new
delivery against that memo, adding only what changed; the OpenTelemetry SDK's
own periodic export does the writing from there. No I/O, and no lock the
callback waits on, runs inside a bus callback. Section 4.7 covers the first
delivery for a session, which this diff rule alone does not settle: a plain
diff against an empty memo cannot tell a brand-new session's opening from a
resumed one's replayed total.

The roots gauge of section 5 does not go through a bus callback at all. It is
registered with `meter.create_observable_gauge` and read on the OpenTelemetry
collect thread, so nothing is added to the writer thread or to the bus for it;
section 5 carries that wiring.

### 4.7 Restart

Both fold families resume from disk, so a counting fold does not restart from
zero. `crew_log/checkpoint.py` carries `CHECKPOINT_DIR` for session folds and
`SLOT_CHECKPOINT_DIR` for slot folds, with `slot_checkpoint_path`, `load_slot` and
`slot_resume_still_verifies` as the slot half; `crew-log-projection.md` section
7.1 is the contract, including the identity block a savepoint must match before it
is admitted.

One consequence for this design: a counting fold keyed by slot is as cheap to
resume as a session-keyed one, so it does not need `mode = "lazy"` to avoid a
cold fold.

The export's own memo (section 4.6) is a different story, because it is
in-memory and does not resume from disk. A session's render stays cumulative
across a gateway restart, but the memo that tracks "what has already been
exported for this session" starts empty again, so the restarted process's
first delivery for any session needs a way to tell a brand-new session's
opening from a resumed or already-evicted session's replayed total. Diffing
every first sight against an empty memo the same way gets one of the two
cases wrong either way: baselining every first sight protects the resumed
session (no re-added total) but drops a new session's own first turns to a
floor it never recovers from, while diffing every first sight against zero
protects the new session but re-adds a resumed session's whole total. Neither
blanket rule is correct; the two cases need to be told apart.

S1 ([#21272](https://github.com/kirodotdev/KiroCrew/issues/21272)) settles
this with one extra fact already on the log: `opened_ts`, the `session/opened`
entry's own timestamp (`Entry.time`), carried in the fold's render. The export
tracks two wall-clock markers, both in milliseconds and both starting at
install time: `_install_ms`, captured once when the subscriber installs, and
`_evicted_max_opened_ts`, which starts equal to `_install_ms` and rises to a
dropped session's `opened_ts` whenever the `_MAX_TRACKED_SESSIONS` (4096) cap
evicts a session whose `opened_ts` is higher than the current marker. A
session's first-sight delivery counts as NEW -- and is counted, never
baselined -- only when its `opened_ts` is strictly greater than
`_evicted_max_opened_ts`: strictly, not `>=`, so a session whose `opened_ts`
ties the evicted marker (the evicted session itself, seen again) reads as
not-new rather than as new. Every other first sight is BASELINED instead: the
current render becomes the floor and nothing is emitted, so a restart or an
eviction replay never re-adds a total already exported. Because both sides of
this test are wall-clock times, it assumes the clock does not step backward
across a restart; the one case where it does is named after the under-count
cases below.

A session that is NEW on first sight is counted WHOLE, diffed against zero
rather than baselined: `session.started` adds one, and every turn already
present in that first render adds its full count (and, for S2, so do the
session's depth and any child it has already created), because a new session's opening and its first turn can arrive in
the same coalesced fold batch, and diffing against zero is what keeps that
first turn from being lost to a baseline. Every later delivery for a session,
new or not, diffs turns against the stored floor the way section 4.6
describes. No I/O runs in the bus callback either way.

One correction against an over-broad claim this document made earlier.
`_install_ms` and `_evicted_max_opened_ts` are the only GLOBAL state the
restart logic adds, and both are plain integers -- but the per-session memo
section 4.6 describes (`_last_values`, keyed by `(fold_name, session_key)`)
does hold the session id, in memory, for as long as that session is tracked
(bounded by `_MAX_TRACKED_SESSIONS`, this section's eviction cap). Its value is
the fold's own render, which by section 4.5 is already the closed
`agent_kind` enum and fixed numbers -- no raw agent name passes through this
memo, because none ever reaches the fold's render for it to copy. That memo
already exists for the restart logic to work at all: diffing a delivery
against *something* requires keeping that something, keyed by session. This
document's privacy claim is therefore about what is BOUNDED (one entry per
tracked session, never a growing list, never written to disk by this module)
and about what the memo's VALUE never contains (a raw name), not about
holding no session identifier at all -- the session id itself is the memo's
key, in memory, for as long as a session is tracked.

The rule is never over-count, with one known exception (a backward clock
step across a restart) named after the cases below. **After a restart, a session that was already
open keeps being used, and the turns it commits land in the first fold batch
this process folds for it; that whole batch becomes the baseline and is not
counted, because it is diffed against nothing rather than against zero.**
Only a turn committed AFTER that first post-restart delivery is counted.
Fixing this needs either re-counting replayed state (which risks the
double-count section 4.6 exists to avoid) or a persisted cursor read at
install from the fold's own on-disk state instead of starting the memo
empty; both are out of scope for this design. This is the under-count by far
the most likely to be hit in practice -- it needs nothing but an ordinary
restart of a gateway with sessions already open -- and it is bounded,
together with two narrower cases:

1. **A session opened before this process's install.** Every session live
   across a restart has an `opened_ts` at or before `_install_ms`, so it
   reads as not-new on this process's first sight of it: baselined whole,
   not just its session start. Neither its session-started count nor any
   turn it had already committed before the restart is counted by this
   process -- the restart under-count above, stated for this specific case.
   The session was already counted once, by whatever process had it open
   before; recounting it here would be the double-count section 4.6 exists
   to avoid.
2. **A never-seen session whose `opened_ts` is at or before some other
   session's already-evicted `opened_ts`.** This needs the 4096-entry
   `_MAX_TRACKED_SESSIONS` cap saturated to occur at all. It reads as
   not-new the same way case 1 does, so it is baselined whole the same way:
   neither its session start nor its pre-eviction turns are counted.

Both land on the same side: a count the export reports as `N` is a floor the
true count never falls below, never a ceiling it can exceed, which is the
direction a monotonic counter can carry safely. That floor is also a choice
this document surfaces rather than settles -- question 9 in section 11 asks
the OSS team whether it is an acceptable one.

**One case breaks the never over-count rule: a backward clock step across a
restart.** `opened_ts` and `_install_ms` are both wall-clock times. If the
system clock steps backward between the old process and the new one (a time
sync correction, a manual change, a host resumed from a snapshot), the new
`_install_ms` can be earlier than the `opened_ts` of a session that was
already open. That session then reads as NEW on first sight and is counted
whole: its session start, every turn already in its render and, for S2, its
depth add and the children it has already created are added again on top of
what the earlier process exported. The over-count is bounded. It needs a
backward step across a restart, it touches only sessions open across that
restart whose `opened_ts` is later than the new `_install_ms`, and each such
session is recounted at most once: after its first sight its render is the
stored floor, and if it is evicted and seen again its `opened_ts` is no longer
above `_evicted_max_opened_ts`. Removing the case needs a log-ordered marker
in place of the wall-clock comparison, which this round does not build. The
design accepts it as a known limit, and question 9 in section 11 asks the OSS
team to accept it together with the under-count floor. The two session tree counters
that S2 ([#21273](https://github.com/kirodotdev/KiroCrew/issues/21273)) adds
ride the same new-vs-baseline test on the same `opened_ts`, so a session's
depth add, and the child adds of a session that creates others, are kept or
dropped by the identical rule its session-start count is, and carry the same
two cases. The child count itself lives in the fold's savepoint, so a creator
resumed after a restart is baselined once and its next child is still counted
at its true ordinal.

One correction is owed alongside the first implementation: the module docstring in
`crew_log/eager.py` describes a savepoint for a slot fold as something that "would
be a store of its own", written before `SLOT_CHECKPOINT_DIR` existed. It exists.
That text is corrected in the pull request that adds the first counting fold,
rather than here, because it is a code comment.

## 5. What this round adds

| Metric | Name | Type | Attributes | Where the number comes from |
|---|---|---|---|---|
| session starts by agent kind | `kirocrew.crew.session.started` | counter | `agent_kind`, folded from `session/opened.agent` per section 4.5 | the `session/opened` entry |
| turn starts by actor and agent kind | `kirocrew.crew.turn.started` | counter | `actor`, the declared `ACTOR_VALUES`; `agent_kind`, the same closed set as session starts, for the session the turn ran in | the `turn/started` entry, attributed from the session's own `otel_usage` fold state (never the raw agent name), so turns inside conductor and worker sessions can be counted apart |
| session tree depth | `kirocrew.crew.session.tree.depth` | counter | `depth`, one of `0`, `1`, `2`, `3+` | the `depth` field on `session/opened`; one add per session |
| session tree children | `kirocrew.crew.session.tree.child` | counter | `parent_kind`, the creator session's `agent_kind`; `nth`, one of `1`, `2`, `3`, `4-7`, `8+` | the `session/spawned` entries on the creator's own log; one add per child |
| session tree roots | `kirocrew.crew.session.tree.roots` | observable gauge | none | `count_roots_with_children`, a new reader S2 adds to the session tree projection, scanned on each OpenTelemetry collect |
| export lag | `kirocrew.crew.export.lag` | histogram | none | the subscriber timing its own callback, sampled only on a delivery for one of its own folds |

Depth is a counter with a bucket label rather than a histogram so the
distribution survives backends that do not keep explicit histogram buckets: a
count per label is a plain sum any backend keeps. The labels are a closed set,
so the attribute's cardinality does not grow with the deepest tree.

The child counter answers how wide trees get without waiting for a creator to
end. `nth` is the child's ordinal among its creator's children, so the number of
creators with at least `k` children is the sum of the adds at `nth >= k`, and the
width distribution is read off a counter. The ordinal is a count the creator's
own fold keeps: `session/spawned` is written on the creator's log once per child
at creation time (`crew_log/entry_types_lifecycle.py`), so the `otel_session_tree`
fold adds one per entry and the export turns the move from one total to the next
into per-bucket adds. No parent link is walked and no child identity is held; the
fold's state is that integer, the session's depth and opening time, and its own
closed `agent_kind`, folded in `step` with the same classifier section 4.5
describes and stored only as the enum. The ordinal is counted per creator
session log, the same unit `session.started` counts, so a slot that moves to a
fresh session log starts a new count.

The roots metric reads the tree structure that already lives in the session
tree projection; it does not keep that structure in a counting fold. This is
the distinction section 4.3 draws, applied to one gauge: **the counting fold
holds only numbers and never a session id, and the roots gauge holds nothing at
all -- it is scanned fresh on each collect.** The projection is already the
place the tree is folded and bounded; the export is one more reader of it, the
same way every dashboard panel is a reader of a projection. So "a counting fold
holds no identifier" and "the roots gauge reports how many roots have children"
do not conflict: the gauge never asks the fold for that number, it asks the
projection, which holds the tree because holding the tree is its job.

A gauge is wired differently from a counter, and in the simplest way the
OpenTelemetry SDK already offers. S2
([#21273](https://github.com/kirodotdev/KiroCrew/issues/21273)) registers it
with `meter.create_observable_gauge`, the same call the existing inventory
gauges use (`metrics/inventory_gauges.py`). The SDK then calls the gauge's
observe function on its own collect thread, once per collect; that function
reads `count_roots_with_children` off the session tree projection and returns
the number. Because the SDK pulls a value on every collect, a quiet interval
costs nothing: the number is folded fresh at collect time, so there is no dirty
flag and no cached last value to go stale. The one case the gauge has no number
for is a tree it cannot trust yet, which is the next paragraph. Nothing is
added to the writer thread or to `crew_log/bus.py`:
the gauge does not subscribe to `TREE_ADVANCED` or to any publish at all.

The scan is bounded. The session tree store walks at most `TREE_UNIT_CAP`
(`crew_log/session_tree.py`, 4096) units per scan, so the gauge's read is a
bounded pass on the collect thread, off the hot path, not on the append path
and not in any bus callback.

Nothing counts roots today, so S2 adds the count: `count_roots_with_children`
in `crew_log/otel_session_tree_fold.py` folds the nodes of the session tree
projection's own reading, and `read_roots_with_children` in
`crew_log/otel_session_tree_roots.py` is what the gauge callback calls. It
reads through the projection's existing reader, so the projection's own brief
read lock is the only lock involved and the gauge adds no state of its own.

**A tree that cannot be trusted yet produces no observation at all, not a
`0`.** The read returns nothing in two cases: the projection is not seeded for
the current store, and the reading it hands back is incomplete. An unseeded
projection would fold another store's state or an empty one; an incomplete
reading is missing edges, which makes a child look like a root and over-reports.
In both the gauge callback emits no observation, leaving a gap in the series,
and a seeded, complete tree with no branched roots is then a real `0` a reader
can believe. This is the `crew-log-projection.md` section 2 rule that absent is
never read as zero, applied to a gauge: the implementation chooses the "report
unknown" side, and question 7 in section 11 asks the OSS team to confirm it
rather than posing the choice.

"Tree size" (how many sessions a tree holds) is not in this round. Counting it
once per tree is the same count-once problem a roots counter would have had,
and a size gauge read off the projection the same way is a reasonable next-round
metric; it is left for the phase that adds it rather than specified here.

The names and attributes in the table match the implementation pull requests
for [#21272](https://github.com/kirodotdev/KiroCrew/issues/21272) (session and
turn counts) and [#21273](https://github.com/kirodotdev/KiroCrew/issues/21273)
(the session tree); if the two ever differ, the pull requests are the source of
truth. This document fixes no histogram boundary: the one histogram left, export
lag, has its boundaries measured against real installs, not chosen in prose. The
depth and `nth` labels are a fixed vocabulary for the question each counter
answers, not a measurement. Names live in the `NS_CORE` namespace that
`metrics/schema.py` enforces.

Two notes on the new field. `session/opened` declares no `depth` today, so
[#21273](https://github.com/kirodotdev/KiroCrew/issues/21273) adds one; it is a
value the writer already holds at the moment it writes the entry, which is why it
is the one addition section 6 allows on the append path. And it needs a name that
cannot be confused with `turn/started.depth`, which is already declared as prompt
depth and means something else.

## 6. The rules that keep it off the hot path

1. **Nothing new on the append path.** The single exception is the `depth` field
   in section 5, a value already in hand at write time.
2. **A counting fold's step adds one or subtracts one.** No scan, no allocation
   that grows with the log.
3. **The bus callback touches memory only.** No I/O, no waiting on a lock. The
   roots gauge adds nothing to the bus at all: it is an observable gauge the
   OpenTelemetry SDK reads on its own collect thread, so its one bounded scan of
   the projection runs there, not in any bus callback and not on the append
   path.
4. **The export reports its own cost** as one of its own metrics, so "it does not
   slow anything down" is a measurement rather than a claim. The lag histogram
   is sampled only when the delivery was for one of the export's own folds. A
   `FoldAdvanced` for another module's fold is matched against the export's rows,
   found absent and dropped before any work, and sampling that would bury the
   real deliveries under near-zero timings of a no-op -- so the distribution
   measures what the export actually did and nothing else.
5. **A counting fold adds no new wake.** Every committed entry already wakes the
   eager worker, because `status` and `class` declare `affects = KNOWN_TYPES`. A
   new fold rides a wake that already happens.

## 7. Phases

Each phase is one pull request, independently shippable and independently
abandonable, because each adds one fold and the metrics that read it.

| # | Phase | Exit criteria | This round |
|---|---|---|---|
| S1 | The export subscriber, plus the first counting fold: session starts by agent kind, turn starts by actor and agent kind ([#21272](https://github.com/kirodotdev/KiroCrew/issues/21272)) | With the gate off, no instrument is created and nothing is exported or recorded; the `otel_usage` fold still runs as an ordinary eager fold, and its state holds only numbers and closed-set values. With it on, the two counters appear in the local sink and the `agent_kind` mapping emits no operator-written name. The export reports its own callback time. | pull request |
| S2 | Session tree: a `depth` field on the opening entry, a depth counter by bucket, a child counter by creator kind and ordinal, and a roots observable gauge read off the session tree projection on each collect ([#21273](https://github.com/kirodotdev/KiroCrew/issues/21273)) | An entry written before the field exists still folds, reported as unknown depth rather than zero. The append path gains no lookup. The roots gauge adds nothing to the writer thread or the bus; the count is scanned on the collect thread (bounded by `TREE_UNIT_CAP`), and no counting fold holds a session id. An unseeded or incomplete tree produces no observation rather than a `0`. | pull request |
| S3 | Sessions open at once | The count returns to zero after every session in a run is closed, including a session that was killed and wrote no closing entry. The source for a session that died without writing `session/closed` is named in section 11, question 6, before this phase is scoped. | later |
| S4 | Work ledger: nesting, item states, and how long a report waits to be accepted | written when the phase is scoped | later |
| S5 | Shared MCP: which route a tool call took | written when the phase is scoped | later |
| S6 | Counts of crew members, dashboards and projects | written when the phase is scoped | later |
| S7 | This document, and the questions in section 11 | A maintainer has answered section 11 or listed what must change. | document |

S3 is deliberately not folded into S1. A session that is killed writes no closing
entry, so an add-on-open and subtract-on-close pair drifts upward forever; its
exit criterion above is the whole reason it is a phase of its own, and section 11
question 6 is the open point that has to be answered before it is scoped.

S1 is blocked on question 1 and question 3 of section 11: both decide what the
first fold is allowed to count.

## 8. Backward compatibility

Compatible. Nothing here rejects, renames or removes anything the code accepts
today.

- No existing metric changes name, type or attributes.
- No existing entry type changes. S2 ADDS an optional field to `session/opened`;
  an entry written without it is read as unknown rather than as zero, which is
  what `crew-log-projection.md` section 2 already requires of a reader ("absent is
  never read as zero").
- A new fold is a new name in the registry. An older copy of the code that does
  not know the name reads no savepoint for it, which is the cold-fold path that
  already exists for every fold on its first read.
- The consent default does not move. `telemetry.enabled` stays false.

## 9. Security and privacy considerations

**A distribution can describe one person.** "How many sessions are usually open"
is a fact about somebody's working day. Three things bound it: the folds hold
counts only and never a per-person breakdown; nothing leaves the machine unless
the operator points `otlp_endpoint` at a collector themselves; and the whole
thing is off by default. Those are real, but they bound exposure rather than
settle scope, which is why question 3 of section 11 has to be answered before S1
merges.

**No free-form value becomes an attribute, and no free-form value is
retained either.** Section 4.5 is the mechanism: the `otel_usage` fold folds
an operator-written agent name to the closed `agent_kind` enum in its own
`step`, the instant `session/opened` lands, so the raw name is a local inside
that call and never reaches the fold's state, render or savepoint. The one
value this round's design does hold, in memory, is the session key the
export's per-session memo (section 4.6) uses to tell one session's delivery
from another's -- not a name, an identifier the design needs to compute a
delta at all, bounded by the eviction cap section 4.7 states and never written to disk by
this module. The `crewmate` bucket READS one more free-form value -- the
opening entry's `slot` field, which carries a member slug -- and retains it
the same way it retains the agent name: not at all. It is a local inside
`step`, read once for a shape check. Member keys, slot keys, working
directories and MCP server names are values a future phase's fold could be
tempted to retain; this round's design retains none of them, and the rule
that confines a future fold
the same way an attribute value is confined -- closed set, classified before
it is stored -- is the bar sections 4.4 (what a counting fold may hold) and
4.5 (closed sets) set for this round, with the memo's eviction cap in section
4.7, and the one
a later phase has to clear too.

**A counting fold is a new reader of crew log entries.** It must read only
declared entry types: `entry_types.py` does not validate an undeclared one, so a
fold depending on an undeclared type would be reading a shape nothing holds it to.

**Cardinality is a resource question as well as a privacy one.** One unbounded
attribute value costs the recorder one cached instrument per distinct value for
the life of the process, as `metrics/schema.py` states. Closed sets are the
control; a cap on how many values are tolerated would not be.

## 10. Alternatives considered

**One export projection for everything.** Rejected in section 4.3: one
`state_version` shared across modules, and every module's step running on every
entry any of them names.

**A synchronous subscriber on the append path.** This is what a system exporting
raw events does, and section 4.2 says why it does not transfer here: our payload
is a summary, so there is a cheaper place to compute it, and the append path's
single `put_nowait` is a property worth keeping.

**Reading the crew log from the export side on a timer.** It would need its own
fold, its own resume logic and its own position tracking -- a second
implementation of the projection layer, free to disagree with the first about the
same bytes.

**A slot-keyed counting fold declared `mode = "lazy"` to avoid a cold fold on
restart.** Unnecessary: section 4.7 shows slot folds resume from disk, so lazy
would buy nothing and cost the export its publish.

**A "session tree roots" counter that counts a root once as it opens.** A
counter steps on each event, and a root's open entry comes before any child
exists, so a counter cannot know at that moment whether the root will ever get
children; counting it later, on a child's open, counts the same root once per
child unless the fold remembers which roots it already counted, which section
4.4 forbids. The roots metric is an **observable gauge** instead (section 5),
read on demand off the session tree projection rather than accumulated: the
projection already holds the tree, so the gauge asks it "how many roots have
children right now" at collect time and holds nothing itself. The same reading
gives a "tree size" gauge whenever a later round wants one, so size is left to
that round rather than forced into this one.

**A process-wide cumulative fold read through an OpenTelemetry observable
counter, instead of a per-session cumulative fold diffed into `.add()` deltas
by an in-memory export memo.** Raised by design review. One global running
total per `agent_kind`/`actor` pair, bumped in the fold's own `step` the
moment an entry commits, would let the SDK pull the current total on each
collect the way the roots gauge above already does, removing the per-session
memo, the `opened_ts` new-vs-baseline test, the 4096-entry cap, and every
under-count case section 4.7 names -- a savepoint resume would be correct by
construction instead of correct because of a markers-and-floor argument.

Not adopted this round, for two reasons this document has not fully worked
through and leaves for design review to weigh rather than deciding here. The
state is not the cost: totals keyed by `agent_kind` and `actor` are a small
fixed set of numbers, the same size however many sessions have existed. The
cost is where such a fold could live. `crew_log/projection_registry.py`
only admits an eager fold named in `EAGER_SLOT_FOLD_NAMES` or
`EAGER_SESSION_FOLD_NAMES` (section 4.3), so every eager fold is scoped to
one slot or one session log, and each of those two families has its own warm
path (the slot memo or the session memo). An eager fold with any other key is
refused at import. A process-wide fold over every session's log would need a
third family with its own warm path, which is new projection-layer machinery
rather than one more counting fold. And a single fold answering for every `agent_kind`/`actor` pair at once is
close to the "one export projection for everything" shape section 4.3
rejects -- not identical (this is one module's own fold, not a projection
shared across modules), but the same question of blast radius applies: does
a global fold's `state_version` bump cost this one module's savepoint the
way section 4.3 says a shared one would cost every module's. Both need an
answer before this alternative can be adopted, not just weighed in the
abstract.

## 11. Questions for the OSS team

1. **Is approval given for a data category or for a named list of metrics?** A
   category -- session, crew and member activity -- lets S3 to S6 land without
   returning here. A named list means each phase comes back for review. We would
   rather have the narrower answer stated than assume the wider one.
2. **May a self-chosen name ever be an attribute value?** This document says no,
   and names the values it keeps on the machine in section 4.5: an
   operator-written agent name is folded to the closed `agent_kind` enum inside
   the `otel_usage` fold's own `step`, so the raw name never reaches the
   fold's state, render or savepoint, and the export's per-session memo (section
   4.6) holds only the session key -- an identifier, not a name -- for as long
   as that session is tracked. Is that the right bar, or is the closed-enum-
   with-`other` rule itself too permissive?
3. **Are workload distributions in scope at all?** Specifically "how many sessions
   are usually open", reported as a distribution with no per-person detail. The
   alternatives are to drop it, or to report it only as an aggregate across
   installs.
4. **Does crew-shaped data need its own consent?** This document reuses
   `telemetry.enabled` and adds no switch, on the grounds that it is the same
   local sink with the same default. A maintainer may read crew activity as a
   different category from process metrics and want its own gate.
5. **Is there anything here that must not be collected even locally**, with the
   gate on and no endpoint configured?
6. **Where should S3 learn that a session died without writing
   `session/closed`?** An add-on-open, subtract-on-close counter drifts upward
   forever across a crash unless something else tells it the session is gone.
   This document does not yet name that source: a candidate is an existing
   reaper or runtime-ownership entry already written when orphaned state is
   cleaned up, if one exists and is itself a declared crew log entry type; if
   none does, S3 needs one added before it can be scoped, which is a question
   for whoever owns that cleanup path today.
7. **Is "no observation" the right answer for a tree that has not seeded?**
   S2 reports the roots count as unknown rather than as `0` while the
   projection is unseeded for the current store or its reading is incomplete:
   the gauge callback emits nothing and the series carries a gap, so a reader
   can never mistake "not measured yet" for "no roots have children"
   (section 5). That follows the rule that absent is never read as zero, and
   it is the side we chose rather than the side we are asking about -- but it
   does mean a dashboard panel over this gauge has to render a gap, and an
   alerting rule over it has to tolerate one. We would rather have that
   confirmed than assume a gap is cheaper for every reader than a `0`.
8. ~~Should there be a `member` bucket in `agent_kind`, and where would its
   signal come from?~~ **Resolved in S1.** A `crewmate` bucket shape-checks the
   `member-<slug>` prefix on the opening entry's own `slot` field (section 4.5),
   a declared field `session/opened` already carries, so the bucket needs no new
   field and no crew-config lookup -- and the slot itself is read inside `step`
   and never stored.
9. **Is the export's under-count floor acceptable, and its one bounded
   over-count case?** The main case: after a
   restart, a session that was already open keeps being used, and the
   turns it commits land in the first fold batch this process folds for
   it; that whole batch becomes the baseline and is not counted, because it
   is diffed against nothing rather than against zero. Only a turn
   committed after that first post-restart delivery is counted. This needs
   nothing but an ordinary restart with sessions already open, so it is the
   case most likely to be hit in practice. Two narrower cases land the same
   way: a session already open at this process's install is baselined
   whole, so neither its session start nor any pre-restart turn is counted
   (case 1, the specific instance of the restart case above); and once the
   4096-entry eviction cap saturates, a never-seen session opened no later
   than an already-evicted one is baselined the same way (case 2). All of
   these are a floor the true count never exceeds, never a double-count
   (the clock-step case at the end of this question is the one exception),
   but a maintainer reading the exported number may not expect "session
   starts" to silently exclude every session alive at boot, or a
   long-lived session's turn count to go quiet across a restart and resume
   from zero rather than from where it left off. Recovering the restart
   case and case 1 needs a cursor persisted across the restart (out of
   scope for this design, per the disposition on #21342); case 2 is
   inherent to "count only what this process sees new" and persists
   regardless. Section 10 records a different fold shape (a process-wide
   cumulative fold read by an observable counter) that would remove the
   floor entirely rather than asking it be accepted, but that alternative
   is not adopted this round and is left for design review to decide, not
   for the OSS team to weigh here -- this question is about the design as
   it stands. We would rather have the floor named and accepted than have
   a maintainer discover it by noticing the counters read low after every
   restart. The S2 counters carry the same floor: `session.tree.depth`
   skips a session alive at boot, and `session.tree.child` skips the
   children a creator alive at boot made in its first post-restart batch.
   Later children of that creator are still counted, at their true `nth`,
   because the child count resumes from the fold's savepoint. One case runs
   the other way and is accepted here as a known limit (section 4.7): if the
   system clock steps backward across a restart, a session already open can
   carry an `opened_ts` later than the new `_install_ms`, read as new, and be
   counted whole a second time. It is bounded to sessions open across a
   restart with a backward clock step, each recounted at most once; removing
   it needs a log-ordered marker in place of the wall-clock comparison, which
   this round does not build.
10. **Is a per-session-log child ordinal the right unit?** `nth` on
   `session.tree.child` counts children within one creator session log,
   the same unit `session.started` counts. A slot that moves to a fresh
   session log starts its count again at `1`, so a long-lived slot that
   delegates across several logs reads as several narrower creators. We
   chose the session log because it needs no join across logs; a
   per-slot count would need a slot-keyed fold, which this round does not
   subscribe to.
