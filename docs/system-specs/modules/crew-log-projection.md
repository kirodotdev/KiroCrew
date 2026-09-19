# Crew Log Projections

## 1. Purpose

A session's crew log is an append-only file (`crew-log-core.md`). Every view of it
is a FOLD: `status`, `usage`, `timeline`, `tools` and `approvals` -- the session
side panel of the RFC's section 5 table. This module is those five folds, the two
read routes that serve them, and the frame that pushes a fold when the file grows.

The split it implements is RFC NFR-2: the backend folds and cuts pages, the
frontend renders and pages and never folds. A client that folded the log would
need the whole file to show one number.

Scope: the SESSION kind. The crew-kind projections (`roster`, `activity`,
`board`, `budget`, ...) are out of scope here because the crew kind has no writer
yet, and a fold with no producer cannot be tested against anything real.

## 2. The fold contract

A projection is a value plus the `seq` it was folded through (FR-5). Two
projections of one unit are therefore comparable, and a client reconnecting
truncates against that number rather than guessing.

A fold is three pure pieces:

| piece | what it is |
|---|---|
| start | the state before any entry |
| step | one entry applied to the state, in place |
| render | the state as the value a reader is served |

`fold(name, entries)` is those pieces run over every entry. The INCREMENTAL form
is the primitive and the whole-file form is one line on top of it, so a resumed
answer and a from-scratch answer come out of one implementation. Two
implementations would be free to disagree about the same bytes, with nothing in
the file to say which is right.

`Checkpoint` is `(name, last_seq, state)` and is JSON-serializable, so a caller
may store it and continue later. The state is deliberately NOT the rendered
value: a fold keeps bookkeeping a reader has no use for -- the open tool calls it
is matching by `call_id`, the attempt an open turn is on -- and keeping the two
apart is what lets the value stay the surface the dashboard reads. Writing that
state to disk is section 7.

`advance(checkpoint, entries)` does not touch its input. It copies the state
first, because these are frozen records and a returned one sharing a mutable dict
with its input would leave that input claiming a seq its state has moved past.

**A replayed entry is refused, not skipped.** Every entry must have a seq
strictly above the last one consumed. The two plausible causes want opposite
handling and `advance` cannot tell them apart: a caller re-reading a page it
already folded would have its totals counted twice, and a caller holding a
checkpoint for a unit that was removed and recreated would have the whole new log
swallowed as already-folded. So it refuses with `bad_data` and naming the
collision, and `fold_session` handles the recreated-unit case itself by
discarding a bundle whose seq is ahead of the file. It also carries the log
file's creation identity (`SessionProjections.origin`, the header's `createdAt`)
and reuses a bundle only when that identity still matches: a log removed and
recreated that has already grown PAST the cached seq passes the seq guard, so
without the identity check its stale state would be folded onto a different
file's bytes. An unknown identity never matches, so an older bundle without the
field falls back to a full rebuild.

**Absent is never read as zero.** `turn/completed` carries `credits` and `tokens`
only on a provider-reported close, so a synthesized closer omits them. A total
that counted those turns as costing nothing would state a measurement nobody
made, so every total in `usage` rides beside the count of turns that contributed
to it (`turns.credits_reported`, `turns.tokens_reported`), and a caller comparing
the two learns what the total covers.

**Nothing is synthesized.** An interrupted turn and an unmatched tool call are
reported OPEN. Closing them is `CrewLog.open(repair=True)`, which appends real
deterministic closers under write ownership; a reader inventing the same fact in
memory would make two readers of one file disagree about one turn.

**An unpairable id is counted, never paired.** `tool/*.call_id` and
`approval/*.approval_id` may be empty, and an empty id identifies nothing --
keying a map by it would make every such call the same call, so one completion
would close a different call's frame. An id longer than `ID_LIMIT` takes the same
path for the same reason: it is retained, so its size is part of the bound, and it
cannot be shortened to fit because two distinct ids sharing a head would collapse
into one identity. Both sides of a pair coerce the id identically, so the call and
its completion always agree on what an identity is. Those are counted
(`tools.unidentified_calls`, `approvals.unidentified_requests`) and left unpaired.

**Every value is bounded, in count and in size.** A projection is pushed over a
socket on each growth, so its size cannot depend on how long the session ran:
`timeline` keeps the newest `TIMELINE_LIMIT` moments and reports how many it
dropped, `tools` details `TOOL_NAME_LIMIT` names while keeping the totals exact
and counting the rest in `names_omitted`, and the open-call and pending-approval
lists are capped with their own omitted counts. The bound is on the RETAINED
checkpoint state, not only the rendered value: a session that leaks never-matched
`call_id`s or `approval_id`s stops retaining them past `OPEN_RETAIN_LIMIT`
(counted in `open_dropped`/`pending_dropped`), a single tool called through many
servers caps its retained server names at `SERVERS_PER_TOOL_LIMIT` and counts the
DISTINCT omitted ones in `servers_omitted`, and `usage` details at most
`MODEL_LIMIT` models while keeping the whole-session totals exact and counting
the rest in `models_omitted` -- so the deep-copied, cached checkpoint cannot
grow without bound over a long-lived session.

A cap on HOW MANY values are retained bounds nothing on its own, because every
one of those values is a string off the wire. Every retained string is cut to
`TEXT_LIMIT` at the point the fold coerces it -- an approval's tool and reason, a
decision, a server, a tool or model name, and the `status` echoes of agent, owner,
slot, cwd, model, provider, stop reason and error -- so a handful of near-64-KiB
strings cannot outweigh the entry budget they are counted against. A field whose
ABSENCE is meaningful keeps it: a close reason, a stop reason and an error read as
`null` when unset rather than as a reason of no characters.

Cutting is safe only for a label that never distinguishes one thing from another.
A string used as a KEY is refused instead of cut: a label sitting exactly at
`TEXT_LIMIT` cannot be told apart from one that was cut, so keying on it would put
two unrelated tools (or models) in one row reporting each other's totals, which is
a wrong answer rather than a big value. `tools.by_name` and `usage.by_model`
therefore give no detail row to a label at that length, and it goes where a label
past the COUNT budget goes: the whole-session totals stay exact, and the label is
reported as omitted detail. An IDENTITY is refused for the same reason at
`ID_LIMIT`, and both sides of a pair coerce it identically so a call and its
completion never disagree about what an identity is.

A count of omitted detail is a count of THINGS, not of the events that mentioned
them: a tool name reaches that path from its call and again from its completion,
and a model reaches it once per turn it ran. Both counts are therefore
deduplicated against a list, that list is itself capped like everything else
retained here, and with the cap reached a label cannot be recognised as one
already counted. `names_omitted`, `models_omitted` and each tool row's
`servers_omitted` stop at the budget rather than climbing past the number of
labels that exist, and `names_omitted_saturated`, `models_omitted_saturated` and
`servers_omitted_saturated` say the figure has become a floor rather than a total.

**A cold fold holds a chunk, not the file.** Five folds consume the same entries,
so a single generator would be exhausted by the first of them and the span has to
be materialized. Materializing the WHOLE span is what a cold fold does most often
-- with no reusable bundle the range starts at seq 1, which is the ordinary first
read for any session -- so the pass is taken `FOLD_CHUNK_ENTRIES` at a time: one
pass over the file, with what is held bounded. Folding a span in pieces is the
same value as folding it whole, because `advance` is seq-anchored and each chunk
is strictly after the last, and each checkpoint takes only the part of a chunk it
has not already consumed -- which is what lets one chunk serve five folds sitting
at different seqs.

## 3. The five projections

| projection | what it answers |
|---|---|
| `status` | Is this session open, and what is it doing: lifecycle, the open turn and its attempt, agent/owner/slot/cwd, current model and provider, turns completed and refused, the last stop reason, dropped writes. |
| `usage` | What it spent: credits and the four token dimensions, per model; the per-turn context bill by source kind from `context/composed`; compaction count and the context they freed; step count and time. |
| `timeline` | The newest turn, lifecycle and cost MOMENTS, oldest first. Message, step and tool entries are deliberately absent: they are the bulk of a log, the page route and `tools` already serve them, and including them would make the timeline a second copy of the file. |
| `tools` | Calls matched to completions by `call_id`: totals, per name, open calls, unmatched completions. An error is `status` in `refused`/`error`/`failed` OR `is_error` true -- two independent signals, and an absent `is_error` is not a claim that the call worked. |
| `approvals` | Requests matched to decisions by `approval_id`: pending, decided, the decision tally, the last decision. No emitter writes these types yet; the fold is against the declared shape. |

## 4. Reads

| route | answers |
|---|---|
| `GET /api/sessions/{id}/crew-log?from=&to=` | The entries in a seq range, oldest first, with every `ref` on the page resolved (FR-4). |
| `GET /api/sessions/{id}/crew-log/projection/{name}` | One fold's `value` and the `seq` it folded through. |
| `GET /api/sessions/{id}/crew-log/projections` | Every fold, keyed by name, from ONE resolution and ONE pass over the unit, so a caller showing them together cannot be handed a mix from two units. Each fold keeps its own `seq`, which differs by design: an entry advances the folds it belongs to and leaves the rest. |

**The BATCH read answers two things the folds cannot.** A fold says what it holds;
it cannot say why it holds nothing, nor whether it was read mid-write. Both fields
are on `/crew-log/projections` alone, because both exist for a surface showing five
folds at once and no caller of the per-name route reads either -- and the settle one
of them needs is a wait charged to every request that carries it. The per-name and
page reads resolve their id the same way; they simply do not report these:

`resolved` -- whether a unit was NAMED for the id sent. An empty fold has two
causes that a reader must not be shown interchangeably: a session with no unit yet --
one that has not run a turn -- and one whose ACP session was torn down (an idle reset,
a model or agent switch, a compaction that recycles it) and whose entries are still on
disk under the retired id. Both arrive as `seq: 0`, so without this flag a surface
reporting "nothing recorded" states a cause as fact about the reader's own data. The
panel says the record is not addressable instead, and names both possibilities rather
than asserting the retired one.

This deliberately does NOT fall back to the persisted session map to find that
retired id. `SessionMap.get` repairs or removes an entry it judges stale, so
consulting it would make a panel READ mutate session state, which is the reason
`crew_log/resolve.py` documents for never touching it. And a retired unit belongs
to a session this slot no longer is: presenting its totals here would imply a
whole-life figure, which needs the lineage pointer (`session/opened.data.previous`)
and a fold that follows it -- neither exists yet (§8).

`writes_drained` -- whether the emitter owed nothing when the fold was taken. An
append is handed to a queue and the entry point returns, so a turn can END with its
last entries unwritten, and the refresh that turn's end triggers would fold a file
the turn has not finished writing. The batch read waits for `emit.flush` up to
`_SETTLE_SECONDS` first and reports which happened; false means the value may be
behind, which the footer says rather than presenting it as current. The wait is
global rather than per session because a batch the writer has already CLAIMED is
absent from the per-session queue and invisible there, so a session-scoped
predicate would report quiet in exactly the case that matters.

**`{id}` is a unit id OR a session key, and both reads resolve it the same way.**
A session's crew log is keyed by the ACP session id the turn path holds, and a
dashboard caller has no way to learn one: it is on no payload the client reads, and
putting it on the wire to let a client rewrite it into a path would widen what a
client is trusted with. So a key that the session registry recognises is resolved
to the unit it is serving through `crew_log.resolve.unit_for_session_key`, and an
id the registry does not recognise -- which is what an ACP id is, since it is not a
session key -- is used VERBATIM. That ordering is what keeps a unit-id-addressed
read working unchanged, and there are now two such callers on main: the
`kirocrew-crew-log` MCP server reads a unit by id through the unit-keyed door
described below, and the `session_projection` frame carries the unit it folded as
`session_id`, so anything taking an id out of a frame addresses by unit id too.
Both responses echo the id the CALLER sent, never the resolved one: a client polling
by key matches the answer to its request, and the internal identity stays off the wire.

**A slot key is resolved to the session its turns RUN on, not to itself.** A
channel-born slot runs its turns on the channel's own session and carries that key
in `linked_session_key` (`slack:<ts>`), so the ACP provider is registered under THAT
key. The resolver is an exact registry lookup whose one retry is the `dashboard:`
form, so a read that passed the bare slot key would miss the provider and fold an
empty record for every channel-linked session -- and never recover, because that
mapping is stable rather than racy. The read therefore asks
`chat_utils.effective_session_key`, the function that owns the mapping, before it
asks the resolver. That stays inside the invariant this path depends on: it is a pure
attribute read, with no disk and no session-state mutation. An id naming no live slot
passes through untouched, which is what an ACP unit id is.

Nothing enforces that a provider's session id can never equal a live session key --
the two are minted by different code -- so the ORDER is what decides a collision,
and it decides it in favour of the registry: an id the registry recognises is
resolved. That is the branch every chat read depends on, and a test pins it, so the
precedence is a decision rather than a side effect of the lookup's fallback.

The resolution is POINT-IN-TIME, and inherits exactly the guarantee
`crew_log/resolve.py` states: it answers which unit a key's work is landing in
*now*. A reset, an agent/model/effort switch, a compaction that recycles the ACP
session and a provider swap all start a new unit, so a key-addressed read after one
of those folds the CURRENT record and not the retired one -- totals drop, and
nothing in the answer says why. A key whose session was torn down and not
re-created resolves to nothing and reads back the empty fold at seq 0, which is the
same answer a session with no entries gets; the difference is not observable from
here. A reader that must span a slot's retired units needs the lineage pointer
(`session/opened.data.previous`) and a fold that follows it, which this module does
not do. The dashboard panel states the limit in its own footer rather than implying
a whole-life total.

Those two are the BROWSER's door: cookie auth, keyed on a session id the dashboard
already holds. A second, unit-keyed door serves the `kirocrew-crew-log` MCP server
over the same `read_page` and projection reads, on the strict internal transport
only: `GET /api/crew-log/sessions` lists units, `GET /api/crew-log/resolve` answers
which unit a caller's key lands in, and `GET /api/crew-log/units/{unit}/page` and
`/projection/{name}` are the unit-keyed forms of the two above. Their gate, and the
argument for granting them to an agent at all, is in
`docs/reference/crew-log/reading-from-an-agent.md`.

A page reports the tail it OBSERVED, not the one its handle remembers. `last_seq`
on a store handle is that handle's own cached figure -- authoritative only for its
own appends -- and a reader never appends, so a writer growing the file after the
handle opened is invisible to it. The pass over the file is live and walks the
whole tail from `from`, discarding what is past `to` rather than never seeing it,
so the real end is observable at no extra cost and both `last_seq` and `next_from`
come from it. Taking them from the cached figure instead would let a page return
rows up to `to` and still report that nothing follows, and a client that believes
it stops paging with entries left unread.

**A seq is only comparable within one file.** The push skips a projection whose
checkpoint has not moved, and a seq alone does not establish that: `fold_session`
refuses a bundle whose origin does not match the file and rebuilds from the start,
so a log removed and recreated can come back at the same terminal seq carrying
different values. The push compares the bundle's ORIGIN first and treats every
projection of a rebuilt bundle as new; comparing seqs alone would suppress every
frame and leave each client holding the retired file's projection, with no later
growth able to dislodge it.

**The push is a per-process singleton, so a restart rebinds it.** A second install
returns the same publisher, and rebinding only the event loop would leave it
holding the retired dashboard state: `_watchers` would count the old hub's sockets
and every frame would go to a room nobody is in, which reads exactly like a session
that quietly stopped updating. The rebind repoints both the loop and the state, and
clears the scheduling flags, which belong to the loop going away -- a timer armed
there never fires and a flush marked in flight there never finishes, so a stale
flag would silence the publisher permanently. The dirty set is kept: those sessions
did grow, the entries are on disk, and the next pass folds them forward.

The RFC spells the range route `/sessions/<id>/ledger`. The feature is named crew
log, and the dashboard mounts its API under `/api`, so the served path is
`/api/sessions/{id}/crew-log`.

A range wider than the store's page cap is CLAMPED rather than refused, and
`next_from` carries the rest: asking for a whole log is a reasonable question and
the answer is pages. `from` defaults to 1 and `to` to one default page.

A resolved ref carries the citation's VERDICT and span -- `{status, entries,
first_seq, last_seq}` -- and never the cited bytes. Those lines are a page of
their own unit, which this same route serves, and inlining them would let one
page carry up to `MAX_REF_SPAN` lines per entry. Identical refs on one page are
resolved once, and a page resolves at most `MAX_PAGE_REFS` distinct refs, past
which the entry keeps its `ref` with no resolution and the page reports
`refs_unresolved`.

**A page and a fold take opposite postures on a type they do not know**, and the
difference is deliberate. A fold passes its vocabulary to `iter_from`, so a
required unknown type raises `unknown_entry_type` (served as 409) rather than
letting the fold answer with a total that line may have changed. A page passes no
vocabulary: it renders history for a person, where an unfamiliar line is a
missing detail rather than a wrong answer, and refusing the page would hide the
history in front of it. That is the posture `crew-log-core.md` section 6 states for
`page` and `resolve`, applied to a range read.

A session with no crew log is not an error: the page reads as empty with
`exists: false`, and each projection is the empty one at seq 0. A session that
ran with `KIROCREW_CREW_LOG` off has none, and the panel renders without
first asking whether the file exists.

Both routes are gated on the DASHBOARD OWNER. `resolve` makes no authorization
claim, because the storage layer has no caller identity to derive one from, and
says the first caller with a permission model owns the question; these routes are
that caller. A crew log holds the session's message bodies, redacted but whole,
so the audience is the person the conversation belongs to.

## 5. The push

A `session_projection` frame carries `{session_id, name, seq, value}` and is sent
to OWNER sockets, matching the read gate: an app token is an authorized socket and
is not the conversation's owner.

The trigger is the emitter's growth signal. `crew_log.emit`'s write-behind
already groups a turn's burst into one drained batch, and
`add_growth_listener` reports that batch -- so a consumer is woken once per pass
rather than once per entry. The listener is REGISTERED rather than imported: the
emitter is imported by the dashboard, so calling a dashboard publisher from it
would close an import cycle and put a reader's name in the writer's code.

The publisher runs the reading half on the event loop, never on the writer
thread: `notify` hands the id to the loop and returns. It then coalesces for
`COALESCE_SECONDS`, folds all five projections from ONE incremental read of the
entries that arrived, and sends a frame only for a projection whose `seq` moved --
re-sending an unchanged value would spend a socket write to say nothing.

A flush pass runs to completion before the next one starts. A growth arriving
during a slow fold does not launch an overlapping pass: two `_publish` for one
session would otherwise share the same prior bundle and race the cache write, so
an older `seq` could be broadcast last. When a pass finishes with more work
marked, it schedules the next pass itself.

Fold state is cached for at most `MAX_CACHED_SESSIONS` sessions; an evicted
session folds from the start on its next growth. When no dashboard user has a
socket open the pass folds nothing, because the state stays cached and the next
growth continues from where it is, so skipping costs no accuracy.

The storage package is imported LAZILY by the handler module, never at import
time. The crew log is optional behind `KIROCREW_CREW_LOG`, this module sits
on the dashboard's boot path, and a gateway launched with the flag unset must not
pay to load a store it will not read -- the same split the emitter keeps, pinned
by a test that imports the module in a clean interpreter.

Installing the push is gated on the same flag, and gated BEFORE the emitter is
imported. `start_dashboard` calls the installer unconditionally, so asking the
emitter whether it is enabled would import it on every disabled launch -- which is
the cost the flag exists to avoid, not a check of it. The variable's name is
therefore spelled in this module and a test pins that spelling against the
emitter's own constant, so the duplication cannot drift unnoticed. With the flag
off the installer builds no publisher and registers no listener.

**A close does not close a turn.** A session cut off mid-turn writes
`session/closed` with no `turn/completed`, and the `status` fold leaves the open
turn standing. Clearing it would assert the turn finished when nothing recorded it
doing so, and would erase the one fact a reader wants from that log: this session
died with work in flight. A reader sees `closed_at` and the open turn together and
can tell exactly what happened. Only `turn/completed` closes a turn.

## 6. The session tree -- the one fold across logs

Every fold above reads its own unit's file and nothing else (FR-4). The session
tree (`crew_log/tree.py`) is the one reader that looks across logs, and it is a
different kind of thing on purpose: the `session_create` edge is recorded on the
CHILD (`crew-log-core.md` section 5), so "which session opened which" is not in
any one log. It is a fold over the collection, the shape dsh's `flattenLineage`
takes over its per-session `parentSession` header field: the record lives on the
child, the tree is a pure function over all the records, and an orphan or a cycle
degrades to root rather than to an error.

**What is read.** For every unit directory under the session root
(`store.unit_dirs`), the HEADER and the FIRST ENTRY of the oldest surviving
segment (`store.oldest_segment`, `store.read_head`) -- one bounded read per log
however long the session ran. That is enough: the emitter writes `parent` from a
process-local mint witness that exists before the child's first turn or never, so
the entry that created the log carries the parent whenever any entry does, and a
re-attach in the same process can only repeat it. A unit is refused the way
`unit_header_slot` refuses one -- a linked entry, a non-session header, a header
whose id does not fold back to its directory name -- and a header with no entry
behind it yet (the create landed, the announce has not) yields nothing and is
read again next scan rather than cached.

**The fold** (`fold_tree`, pure; input order does not matter):

| Case | Node |
|---|---|
| no record of the slot carries `parent` | root, `parent: None` |
| some record carries `parent` and a log with that slot exists | the edge is followed: the child nests under the creator |
| the cited slot has no log of its own (orphan) | root; `parent` kept as the citation |
| the edges close a cycle, or a slot cites itself | every member is marked `cycle: True` and nests nowhere; a slot hanging off a member keeps its edge to it |
| two records of one slot disagree | the OLDEST log's word stands (`createdAt`, then id); a slot that carries a `parent` at all is one `session_create` minted (`chat-<N>-<ts>`: a monotonic counter plus the unix second, the counter reseeded past every restored key at boot), so such a key is never a dead session's recycled one, and the oldest word is the creation's own |
| a record with no slot in its header | dropped -- it has no place in a slot-keyed tree |

A record without `parent` never retracts one: create -> re-attach (with parent)
-> gateway restart -> re-attach (no parent, the witness is gone) folds to the
parent the first log recorded, and so does a slot whose later logs were opened
after a restart. The tree is keyed by slot and reads `parent.slot` only:
`parent.sid` on the entry is the creator's ACP session id at the moment of
creation, an audit citation for a reader of the logs themselves (`crew-log-core.md`
section 5), and a slot outlives its ACP session, so it is not what a live row
nests on. Nothing reads it today; a reader that shows a session's own log would.

**The cache, and its bound.** `SessionTree` keeps one head per unit directory,
validated per scan against the segment path and its `(st_dev, st_ino)`. No mtime:
the store never rewrites a written line, so the two lines a scan reads are
immutable for as long as the segment exists, and an mtime key would re-read a live
session's log on every append. An untouched unit costs one `stat`; a segment that
is gone (retention, removal) or replaced (a new inode under the same name) is
re-read; a unit that yields nothing is dropped from the cache. A read that fails
outright (an `OSError` after the `stat` succeeded: a moment's I/O fault, or a unit
retention removed between the two calls) is no verdict on the bytes, so nothing is
cached for it: the next scan reads the unit again, or finds it gone and evicts it.
A cached failure would hide that session's creator until the segment rolled or
the process restarted.

A scan ADMITS at most `TREE_UNIT_CAP` (4096) units, and the cap cuts EVERY loop
of the scan, not only what it retains: the live sessions' logs are probed first
(the sampler names them by ACP session id, `store.unit_dir_for`, one `stat`
each, through `islice(preferred, cap)` so absent ids cost no more than the cap
in probes); the store's listing (`store.unit_dirs`, in the directory's own
order, excluding what is already admitted) fills the rest of the cap and stops
one candidate past it; the cache holds one head per admitted unit; and every
string a head retains is bounded at admission (`MAX_ACP_SESSION_ID_LEN` for the
id, `MAX_SHORT_STRING` for the slot keys; an oversize value refuses the unit
rather than truncating to a key that matches nothing). What lies past the cap is
neither walked, read, cached nor counted -- counting it would mean walking the
population, which is the cost the bound refuses -- and THAT something lies past
it lands in `SessionTree.over_cap`, reported on every payload as
`totals.lineage_over_cap` beside `totals.lineage_cap` (the constant, so the
page can say "4,096+"). The Sessions table's footer shows that as an ordinary
stat, "Stored session logs", not in the page's warn colour, only while it is
true: it removes no row from the page, so it is information about the store, and
its label names logs on disk because the strip already counts sessions, task
sessions and session procs, and a fifth "session" figure would read as a fifth
live count. Its hint names the cap itself ("more than 4,096 exist", the value
interpolated from `totals.lineage_cap`), since the bubble opens away from the
stat it explains, and leads with what it means (old logs are piling up), says
what it can cost the page (below), names where its remedy is typed ("in a
terminal run:"),
and what to do (the `kirocrew config set` command for the retention setting,
named as the one switch that also expires the transcripts the Archive page
lists, since `store.sweep_expired` runs off the same value), stating no default,
since the default lives in `config/sections.py` and prose restating it would go
stale silently. Because the live logs go first, what the cap leaves unread is
closed sessions' logs, and a live row nests on one of those in exactly one
case: a slot that outlived a gateway restart, whose current log was opened
without a `parent` (the witness is gone) and whose creator is named only by its
older, closed log -- a unit that competes in directory order like any other and
can fall past the cap. Such a row folds as a root while the store is over the
cap, which is why the hint says a session restarted since it was opened may show
as top-level instead of under its opener, rather than that nothing on the page is
affected. The hint also says what the retention command removes and keeps, since
a reader who fears losing transcripts will not run it: only closed sessions' logs
and the old saved transcripts (the rotated archives) older than the days set go; running sessions
and anything newer stay (`store.sweep_expired` removes only a unit whose close is
terminal; `history._cleanup_old_archives` deletes only rotated archive files). The
Storage screen's age sweep is not the remedy for this pile: it moves transcripts
and kiro-cli replay logs to the Trash and never touches a crew-log unit, so a hint
that sent the reader there would promise a shrink that does not happen. Every
other live row folds, over the cap or not -- with one bound: the preferred set
is capped too, so a gateway running more live logged sessions than the cap
loses lineage on the rows past it. A unit that fell past the cap because the
population changed is evicted like a removed one. What a poll costs at the cap,
measured on a local disk with 4,096 units: the cold first scan (one root
listing, one listing and one head read per unit) took 376 ms; a warm scan (the
root listing, one listing and one `stat` per unit, every head from the cache)
took 90-120 ms, and the fold on top of it is within that. The sampler runs the
scan on the executor beside its other filesystem work, and the page polls every
5 s, so a store at the cap costs about 2% of one core while the Sessions tab is
open and nothing while it is not. The cap is far above any population retention leaves; a
store that reaches it usually has retention disabled, though a store with more
than that many unexpired logs reaches it too. `test_crew_log_tree.py` measures
the invariant rather than reading it off the code: a scan handed ten times the
cap in absent ids makes exactly the cap's worth of probes, a store three times
the cap is examined for cap + 1 candidates, and the cache never exceeds the cap.

The scan is blocking and runs where the sampler's other filesystem work runs, on
the subprocess executor, never on the event loop. A scan that raises is logged and
reported as an empty tree: the tree decorates the pages that show it, and a store
fault must not take them down.

**The wire.** Each session row of `GET /api/sessions/memory` carries `parent`:
`null` for a session nobody created, otherwise `{slot, key}` -- the cited creator
slot, and `key` the creator's LIVE session key when the creator is running and the
edge can be followed (`null` for a creator that is not running, a node on a cycle,
or a citation pointing at the row itself). The join from a log's slot to a live
row is by slot key alone: a dashboard row's key is `dashboard:{slot.key}` and its
log -- and any child citing it -- carries the bare `slot.key`. `totals` carries
`lineage_over_cap` and `lineage_cap` (above); the sampler hands the tree the live
rows' ACP session ids (`runtime_pids` carries each as `sid`, bounded by
`MAX_ACP_SESSION_ID_LEN` at retention) so those logs are read first. The Memory column's hint says each row is its own runtime's figure, a parent's figure does not include the rows nested under it, and a group's header row under Group by is the one row that does total (TanStack's sum aggregation on the grouped column, which is the base table's behaviour), so the reader is not left to guess which bold rows sum. A task row carries a muted "task" marker before its name: once created sessions nest too, indent alone no longer says which kind an indented row is, and the kind otherwise showed only on hover (a session's name underlines, a task's does not). A folded session's count is the visible text "M MB in N hidden rows", unit included and the memory bound to the rows in the words (beside the parent's own Memory cell a bare "N rows, M MB" left the reader unsure which figure was whose): it counts sessions and tasks, where the footer's "nested" counts sessions only, and a bare numeral beside that reads as either; the memory is the hidden rows' own figures summed, carried on the badge because a folded parent's figure is its own and without the roll-up beside it the fold reads as a family total (a row with no memory data contributes nothing, and a fold with none shows the count alone). The
System page's Sessions table nests a session under `parent.key` exactly as it
nests a task under its `parent`, to whatever depth the creating went, with a task
under whichever session spawned it wherever that session sits; a created session
whose creator is not running is a top-level row that still carries its citation.
A row nested under its creator needs no further citation: its place in the tree
is one, and the creator's expander names the relation ("Collapse sessions under
{name}"). A created row that could NOT be nested (creator not running, a cycle)
says who opened it as VISIBLE text under its name -- "Created by {creator} (not
running, so shown top-level)", the creator's display name when it has a live row, else the slot the
log cited; the parenthetical names the one reason a created row is top-level
that real creation order can produce (a cycle is the other, and cannot arise
from ``session_create``, which never lets a child create its own ancestor) --
never as a native `title`: a keyboard or touch reader sees no tooltip, and this
row has nothing else that says it. The table re-checks the edge it is handed --
a key naming no row in the payload, or a chain returning to its own start --
because a table must never fail to paint on a payload it did not produce.

## 7. Savepoints on disk

A fold is cheap per entry and unbounded in total, so folding from seq 1 makes the
projection route cost what the session's whole history costs. The push avoids that
with the in-memory bundle above, but that cache dies with the process and holds
`MAX_CACHED_SESSIONS` sessions, so a restart and an eviction each pay for the file
again. `crew_log/checkpoint.py` is RFC NFR-1's answer: each fold's state written
beside the log it came from and resumed on the next read. Measured on a
10,001-entry (1.48 MB) log, all five projections, best of five runs on one host:
184.4 ms to fold cold, 44.8 ms to resume with nothing new to fold, 105.5 ms to
resume with a short tail, 197.1 ms on the read that also earns a new savepoint,
and 24 KB of files. Read those as ratios on one host rather than as portable
constants.

An earlier revision of that sentence said 3.2 ms to resume. It was measured before
the prefix digest existed and is no longer true of any resume: the digest is
verified on the way in and rechecked after the pass, and those two hashes are
essentially the whole of the 44.8 ms. Section 6 gives the attribution.

One file per fold, inside the unit's own directory, which the RFC's section 3
already names:

```
<store dir>/projections/<fold>.json
{"v", "unit", "origin", "first_seq", "fold", "seq", "prefix_sha", "prefix_records", "state"}
```

One file per fold rather than one for all five, so a payload this build cannot
read costs that fold its savepoint instead of costing all of them, and so a caller
asking for one projection writes one file. The name is a fold name that passed
`require_name`, so it can only ever be one of the five words this package
declares. The store reads its segments by name (`log.jsonl`, `log.<first_seq>.jsonl`)
and ignores every other neighbour, and removal deletes the unit's whole directory,
so the files need no registration on either side.

**Disposable, and that is the property to keep.** Every failure -- no file, a
truncated one, a payload from a build this one does not understand, a store the
file no longer describes -- is answered by folding from seq 1, which reaches the
same value at more cost. `load` and `save` therefore never raise: nothing a reader
is served depends on a savepoint existing or being current, and the tests state
each rejection as "the fold still lands on the cold answer".

**An append-only prefix never invalidates one.** The entries a savepoint consumed
cannot change, so folding what came after reaches what a cold fold reaches -- the
section 2 equality, now with a file behind it. Four things break it, and each is
checked before a file is used:

| check | what it catches |
|---|---|
| `origin` | a unit removed and recreated under the same id. Its seqs start again, so once the new file grows past the stored seq a seq check alone passes. It is the same value `SessionProjections.origin` compares, spelled once in `log_origin`, because two spellings of "same log" could disagree and the lenient one would fold a retired file's state onto a live file's bytes. |
| `first_seq` | the log lost its FRONT. Retention deletes whole segments off the oldest end, so a cold fold now folds a window while the savepoint still counts entries that are gone. The savepoint's answer is the one no reader can reproduce, so it is the one that is retired. |
| `seq` vs the log's end | a store SHORTER than the savepoint. Mostly caught by the two above, and checked on its own because a fold resumed past the end of a file is the one state no later read recovers from. |
| `prefix_sha` | an entry BELOW the savepoint's seq that changed after it was folded. The three checks above read the log's identity, its front and its length, and none of them reads the consumed prefix -- so without this one a savepoint and a cold fold disagree in exactly one case, and the savepoint is the answer that looks clean. `store._iter_entries` documents that a damaged interior line is SKIPPED on purpose, so a cold fold silently omits that entry while the savepoint keeps the value it folded. The digest is over the RAW RECORD BYTES of the consumed prefix, so it catches a rewritten line, a newly damaged one, and a newly readable one alike. It is checked TWICE on a read that resumes: once before the pass, and again after it, because the pass consumes entries above the prefix and damage landing in between would otherwise leave the served state carrying a record the file no longer yields. The second check is the same one -- `checkpoint.resumed_prefix_still_verifies` re-runs the load rather than re-implementing the comparison -- and a mismatch retries cold through the same path an identity change uses. |

**The identity is also read AFTER the pass, and a change discards the fold.**
`iter_from` opens the log by NAME, so a unit removed and recreated between the
identity read and the read of the entries hands the fold a different file's
entries while it holds the first file's state -- and the seqs do not say so,
because a recreated log starts its own again. `fold_session` therefore folds once
more from scratch, and on a second change reports `origin: None`, which is
"unknown identity": it is what stops a caller reusing the bundle and stops it
being written, since both compare against that field and neither accepts `None`.
The value is still served, because refusing to render a session that exists is the
worse answer.

That after-check is only as good as the identity it compares, so `log_origin` reads
BOTH signals from the file on disk: the file's device and inode, and its `createdAt`
through `store.unit_header_created_at`, which parses the header line itself. Neither
comes from the handle's own parsed header. That header is read once when the handle
is opened, so a handle held across a recreation would keep reporting the retired
file's stamp -- leaving device and inode as the only live signal, and those agree
whenever the new file lands on the freed inode, which is the common case rather than
a rare one. An earlier revision of this section said the savepoint FILES were exempt
because `load` and `save` hold a freshly opened handle. That was wrong: both take
the handle their caller passes, which is the handle `fold_session` folds with, so
the disk read is what protects them too.

**The prefix digest is what makes "a consumed entry cannot change" checkable rather
than assumed, and it is affordable because folding is not the same work as reading.**
`CrewLog.raw_prefix_digest` walks the unit's segments in the order
`segment_paths` gives them, skips each segment's header record, and hashes the next
`prefix_records` raw records. Three things follow from doing it that way. It decodes
no JSON, which is the point: on the log section 7 measures, hashing the whole prefix
is 20.6 ms against a 184.4 ms cold fold of all five projections. That ratio is the
weaker of the two honest readings, though, because a cold fold is not the read the
guard runs on. On a RESUME it is not a rounding error: the digest is verified on the
way in and rechecked after the pass, which is why an otherwise idle resume spends
44.8 ms almost entirely on two hashes, and a resume that also earns a write resolves
the record count and hashes again -- 66.6 ms for `prefix_witness`, 20.6 ms for the
recheck -- and lands at 197.1 ms, about what folding cold costs. The savepoint still
wins on the reads that dominate, the ones with nothing or little to fold, but by
roughly 1.7x to 4x rather than by an order of magnitude. An earlier revision of this
section quoted 343.6 ms for that cold fold and called the guard 6% of it; that figure
disagreed with section 7's own measurement of the same log, and both are replaced by
the one run quoted there. The boundary it is handed is a RECORD count,
and the FOLD resolves that count, from the last seq it read before its pass, through
`CrewLog.raw_records_through`, which is the one place a seq is read. And the framing is
the store's own rather than a second copy in the savepoint module, because this is the
path that decides whether damaged bytes are trusted.

**The digest is read BEFORE the pass, and the write persists that reading instead of
taking one of its own.** A digest read at write time covers whatever the file holds
THEN, which need not be what the fold consumed: a consumed record that changed in
between would be hashed together with state folded from its earlier value, and because
the recorded digest and the recorded state would then agree with EACH OTHER, every
later resume would recompute those same changed bytes, match, and serve state a cold
fold disagrees with -- for the life of the unit, since nothing rechecks a digest that
verifies. So `_fold_attempt` reads it through `checkpoint.prefix_witness` before it
consumes anything, `held_still` asks `checkpoint.prefix_unchanged` once the pass is
done, and `save` writes the value it was handed. A pass that ends at some other
boundary, because the file grew under it or because the handle's own seq was stale,
matches no fold in the bundle and writes nothing: that costs the savepoint, never the
answer the read serves.

**Only a read that folded the prefix ITSELF may write a savepoint, so an incremental
read serves from its caller's bundle and persists nothing.** The custody the paragraph
above describes has to reach across calls as well as across a pass. A read that resumed
from DISK carries it transitively: the savepoint records the digest its own writer read
before folding, and `checkpoint.resumed_prefix_still_verifies` checks that recording
again, so the digest on disk is still evidence about the bytes the state came from. A
`since=` bundle carries no digest -- `SessionProjections` has no such field -- and the
bytes below its seq were consumed by a call that has already returned, so nothing the
new pass can read is evidence about them. A digest taken then would be honest about the
file and wrong about the state beside it, which is exactly the pairing that makes a
wrong answer permanent. So `_fold_attempt` takes no witness on that path, and `save`
writes nothing without one. The cost is that a hot incremental reader -- the dashboard
push loop in `dashboard/handlers/crew_log.py`, whose `MAX_CACHED_SESSIONS` bundles are
folded with `since=` on every publish -- brings its savepoint forward on the next read
that folds the prefix instead of on every read, which is the lag the section below
already allows for.

An earlier revision of this section derived the boundary as `last_seq - first_seq + 1`
and justified it by saying the log is append-only with contiguous seqs, so a count
names the same boundary and a damaged line still occupies its slot. That was wrong,
and a reviewer caught it. A SUBSTITUTED damaged line does occupy its slot, but a blank
or unparseable line that the entry reader skips is still a record to the raw walk, so
the entry span and the record count diverge by one per such line. Handed the entry
span, the walk stopped that many records short, and the trailing records the fold HAD
consumed fell outside the digest -- where damage to them passed every guard, since
identity, first seq and length all still matched. `raw_records_through` decodes a
record per line, so only a fold that owes a write calls it; a savepoint is written
rarely, and the reader is handed the resolved count, which keeps verification
decode-free.

The honest cost of this, corrected after a reviewer caught the first version of this
sentence overclaiming: a savepoint saves the FOLD work, not the parse. `iter_from`
walks `_iter_segments` and drops entries below the requested seq AFTER constructing
them, so a resumed read that has anything to fold still decodes the prefix it skips
folding. What the digest adds on top is a second pass over the same bytes, hashing
only, which is why it is cheap next to folding and not free. The header record is
excluded because `origin` already compares creation identity and including it would
blur which guard fired. The three cheap checks stay in front of the digest even though
it would catch their cases too: they are O(1) comparisons, and a rejection should not
pay a pass over the whole file.

**A savepoint is allowed to LAG, and that is what keeps the write off the hot
path.** One is written only once the bundle has advanced `MIN_ADVANCE_ENTRIES`
past what is on disk; resuming from an older one replays the tail and reaches the
same value. Without the threshold the push would rewrite five files each time a
session grew by one entry, which is the cost this removes rather than relocates.
It also means a short session leaves no file at all: folding it from the start is
already cheap. `SessionProjections.saved_seq` carries what is on disk, so a caller
reusing a bundle decides from what it holds instead of reading the files to find
out.

**The payload is ASCII-only.** A crew log's own JSON admits a lone surrogate, so a
fold can retain one in a label -- and a serializer that passes it through makes the
UTF-8 encode raise out of a function that promises never to. Escaping every
non-ASCII character round-trips the surrogate and cannot fail, which is what the
store's own serializer does.

**No fsync.** A savepoint a crash leaves unpersisted is an older savepoint or no
savepoint, and both are answered by folding further, so a flush per write would
buy nothing the cold fold does not give for free. The rename is still atomic,
which is what keeps a reader from seeing half a payload.

**The write goes through the unit's lease, non-sole.** Nothing here needs
ownership to be correct against another READER: each file names the log and the
seq it describes, so any writer's version is a valid savepoint of the same
append-only bytes. Removal is the different case. It takes the lease `sole`, which
`acquire` refuses while any other hold exists, so holding a shared one across the
create, the write and the final check is what stops a removal starting in the
middle of them -- and a removal already in progress refuses the reader instead,
which is the answer that leaves the removal whole. Contention is a reason to skip,
never to wait: the read the fold was for is already served.

Two more rules close the ends the lease cannot. Establishing the identity stats
the newest segment, so a removed unit fails there -- BEFORE the lease, which would
otherwise create a lease file inside a directory removal has already emptied. And
after the write, a unit with no segment has everything just written deleted again,
the unit directory included: `atomic_write` creates its target's parents, so a
write that landed after a removal emptied the tree rebuilt that directory too, and
nothing else collects an empty one, because the retention sweep decides from a
unit's own entries and a unit with no segments has none.

The size cap is a BACKSTOP on section 2's bounds, not a bound itself: a fold that
grew unbounded state loses its savepoint instead of writing an unbounded file on
every read.

**Changing what a fold stores bumps `CHECKPOINT_VERSION`, and a test enforces
it.** `CHECKPOINT_VERSION` and `_state_matches_fold` both check the payload's
SHAPE, so the case neither sees is a fold whose MEANING changes while its keys do
not -- a counting fix in `usage` or `status` being the likely one. The old build's
savepoint then resumes onto the new logic, and the long sessions this exists to
speed up are the ones that keep serving pre-fix numbers for the life of the unit,
with no in-product way to retire the file because the tree is fenced from the
agent. So the rule is: any change to what a fold's `start` or `step` stores bumps
the version, which retires every savepoint to a cold fold at one refold each. The
rule is not left as this paragraph --
`test_changing_what_a_fold_stores_forces_the_savepoint_version_to_move` digests
each fold's stored state over a fixed script with the clock frozen, so a changed
fold reddens CI with the bump named in the failure. One global number over a
per-fold one is deliberate: it over-retires, and over-retiring costs a refold
while under-retiring serves a wrong number.

## 8. Deliberately not here

- **A savepoint that survives a segment rollover.** `origin` carries the newest
  segment's inode, so a log that rolls over retires its savepoints once. No writer
  creates a second segment today, and whoever adds one has to revisit `log_origin`
  anyway -- the in-memory bundle reuses the same identity and has the same
  weakness. The cost of leaving it is one cold fold per rollover.
- **Crew-kind folds.** No crew writer exists.
- **Subagent lineage and fork pointers.** A `subagent/spawned` entry's `ref` is
  resolved on the page like any other citation. The session tree (section 6)
  folds the `session_create` edge only: a `spawn_run` subagent has no session
  log of its own to record a parent on, and a fork stamps no creator.
- **SPA rendering.** The frame shape is specified here so the client can follow.
- **`turn/completed` carrying `attempt`.** It does not, so a fold cannot pair a
  completion with its start by field. The pairing is positional: a `turn/started`
  opens the current attempt at that ordinal and the next `turn/completed` for it
  closes whatever is open, which is what the file supports.
