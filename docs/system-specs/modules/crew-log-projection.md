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
apart is what lets the value stay the surface the dashboard reads.

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

## 6. Deliberately not here

- **Checkpoints on disk** (`projections/<key>.json`). State is kept in memory,
  keyed by session, and the shape is already the one a checkpoint file would
  carry, so persistence is additive.
- **Crew-kind folds.** No crew writer exists.
- **Subagent lineage and fork pointers.** A `subagent/spawned` entry's `ref` is
  resolved on the page like any other citation; walking the tree is its own work.
- **SPA rendering.** The frame shape is specified here so the client can follow.
- **`turn/completed` carrying `attempt`.** It does not, so a fold cannot pair a
  completion with its start by field. The pairing is positional: a `turn/started`
  opens the current attempt at that ordinal and the next `turn/completed` for it
  closes whatever is open, which is what the file supports.
