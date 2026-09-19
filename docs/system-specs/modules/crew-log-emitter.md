# Session Crew log Emitter

`crew_log/emit.py` turns the gateway's ACP turn lifecycle into the append-only
per-session crew log `kiro_crew.crew_log` keeps for each ACP session id. It is a **writer
only**: it owns which facts matter and where they are known, not storage or its layout.

The emitter is gated behind `KIROCREW_CREW_LOG=1` (`constants.env_flag_enabled`,
read per call, truthy set `{1, true, yes, on}`) and defaults **OFF**, so this module is
inert until a later change turns it on. With the flag off no crew log directory is created
and no call reaches the storage library.

There is ONE flag. An earlier design gated streamed deltas behind a second one; that
emitter does not exist, for the reason "Bodies, and the flag" gives below, so there is
nothing left for a second flag to govern.

**Off is free, not merely silent.** Every entry point that hashes a payload or redacts a
body checks the flag ITSELF, before doing that work -- `on_tool_called`,
`on_tool_completed`, `on_message_received`, `on_message_sent`,
`on_request_configured`. Hashing is proportional to payload size and
redaction walks the whole body, both once per frame on the event loop, so leaving the check
to the writer would charge every user for a feature that is off by default.

## Identity

The crew log key is the **ACP session id**, not the slot key. A slot outlives its ACP
session: an agent switch, a model switch, or a project change calls
`SessionLifecycle.reset`, which pops the live session and lets the next turn cold-start a
fresh one. When that reset cleared the conversation the successor has a new id and a new
crew log; when it did not, the next cold start resumes the same id via `session/load`, so
`CrewLog.exists` decides between create and open and a resumed session never truncates it.

A create that a slot's PREVIOUS crew log already exists behind is the supersede case, and
the successor records it: `session/opened.data.previous = {sid}`. The id comes from
`SessionManager.mapped_sid`, the slot's session mapping read without pruning, latched by
whichever allocation observes it first. One limit is recorded rather than worked around: an
allocation whose replay is still pending does not publish its fresh id over the mapping, so for
that window a mapping read names the crew log BEFORE the newest one, and two successive crew
logs cite that same predecessor while the crew log between them is cited by nobody -- a chain
walker steps over it with no signal that it did. Closing that needs a deferral that resumes
once the predecessor's own writes settle, which is tracked with the rest of the supersede work
in #12148. `mapped_sid` rather
than `resumable_sid`: the latter asks "can this id still be resumed", so it stats the ACP
transcript on the calling thread (a sync store read the turn coroutine must not make) and
PRUNES the entry when that file is gone or empty, which erases the id exactly when the two
stores disagree -- and a crew log unit outliving a truncated ACP transcript is the unit whose
tail most needs closing. A successful resume answers the same id, so the emitter compares and
writes no self-edge. This is the read side's only way to join one slot's crew logs in order;
`resumed` cannot, since a superseded session has a different id and therefore a different
unit.

`owner` and `agent` are header fields, written once at create time. There is no turn id in
the repo, so a turn is identified by its message boundary, `len(slot.messages)` at turn
start, and every entry of that turn carries it as `data.turn`. That ordinal, plus
`data.step` on a tool call, is the grouping identity: both are known at emit time, so an
entry answers what happened in its turn on its own. The envelope's `thread` field is left
unset here -- see "Reconnect is not resume" below for what it is for.

## Emitted facts

| Fact | Site | Data |
|---|---|---|
| `session/opened` | after `get_or_create`, on create or re-attach only | agent, slot key, model, `model_requested` when a tier resolved one, cwd, `resumed`; `previous {sid}` on a CREATE whose slot was mapped to a different session id, from `mapped_sid` (in-memory, non-pruning); a replay-pending allocation defers publishing its fresh id, so for that window the mapping names the crew log before the newest one and the one between is cited by nobody, which is recorded as a residual on #12148 rather than handled here; latched on the slot by whichever allocation observes it FIRST -- the eager prefetch maps its own session over the key before the first turn runs, so a turn reading the mapping for itself would answer the successor and write no edge; the latch is write-once and is spent on one entry; recorded only when the named crew log's own header names this slot, read at emit time, so a stale or recycled mapping entry yields no edge; `parent {slot, sid?}` when `session_create` made the session IN THIS GATEWAY PROCESS (`_lineage_minted`) -- the creator's key from the slot's `_created_by`, and the creator's ACP session id FROZEN at mint (`_created_by_sid`) from the live caller handle, present when the caller had a session at that moment; a slot restored from transcript metadata writes no `parent` |
| `turn/started` | after every dispatch gate, immediately before the stream opens | turn ordinal, actor, prompt depth |
| `turn/refused` | each gate that refuses the dispatch | turn ordinal, actor, `reason`, prompt depth |
| `turn/completed` | the `EVENT_COMPLETE` arm, beside `_emit_turn_metric`; the turn's `finally` when no terminal event arrived | the four `TurnUsage` token counts, credits, `duration_ms`, `stop_reason`, model, provider -- or `stop_reason: "failed"` with `error` and no usage |
| `tool/called` | `EVENT_TOOL_CALL` | `tool_call_id`, trusted `tool_name`, `mcp_server_name`, kind |
| `tool/completed` | `EVENT_TOOL_RESULT` when `tool_final` | same id, outcome, elapsed ms measured by the emitter |
| `model/selected` | the fallback swap in `_run_chat`, after the pick lock is released | model id, source, turn |
| `compaction/applied` | `_settle_compact_cooldown` when the after-reading is confirmed; `_compaction_gate_decision` when a deferred verdict settles | `pct_before`, `pct_after`, freed |
| `session/closed` | `SessionLifecycle.reset` | the gateway's own `end_reason` |
| `message/received` | `_run_chat`, before the dispatch gates | turn, role, redacted text, surface, attachment ids |
| `message/sent` | `_flush_segment`, beside the assistant `slot.append`; `_persist_partial_reply` on a recovery path | turn, step, redacted text or cited `chunks`, `interrupted` |
| `message/chunk` | the overflow split ONLY | turn, step, one slice of an already-redacted body |
| `request/configured` | `_run_chat`, on change only | turn, model, provider, `context_window` |
| `context/composed` | `_run_chat`, from `slot_ctx_blocks` | turn, per-block `sources`, chars, estimated tokens |
| `step/started` | the stream opening, and each tool-group-to-text transition | turn, step |
| `step/completed` | the next transition, and `EVENT_COMPLETE` | turn, step, ms |
| `message/queued` | `chat_delivery.queue_for_next_turn` | source, bytes, queue id -- no turn |
| `approval/requested` | `_run_chat`, one statement before the try whose `finally` records the decision | turn, request id, tool name and the redacted title the human is shown, each omitted when the frame named neither |
| `approval/decided` | the same prompt's `finally`, where every exit converges | turn, request id, decision, `by: host` and the host's `cause` for an auto-decline |
| `plan/updated` | `EVENT_TODO_UPDATE`, inside `slot.set_todo`'s own change gate | turn, the whole task list as `{id, text, state}` |
| `background/completed` | `run_bg_oneliner` and `background_turn`, at the point they record usage, against an owner pinned BEFORE the call | kind, served model, provider, the billed token dimensions, credits, ms -- no turn |
| `subagent/spawned` | `_log_spawned`, the one site every started run passes and no rejection does | the turn that ASKED, read from the pin taken at acceptance; child id, agent, model, the three context-scope flags |
| `subagent/steered` | `steer_run` after the provider accepted, `follow_up_run` after the queue accepted | child id, `interrupt` or `follow_up` |
| `subagent/completed` | the exclusive terminal report, for outcome `completed` | child id, elapsed ms |
| `subagent/failed` | the same report, for outcome `failed` or `stopped` | child id, reason, which outcome it was, elapsed ms |
| `write/dropped` | writer recovery, before that session's next ordinary append | dropped count and bytes |

`tool/called` and `tool/completed` gained payload accounting: `args_hash` and
`args_bytes` on the call, `result_hash`, `result_bytes` and `is_error` on the
completion. The digest stands IN PLACE OF the payload, never beside it -- arguments
and results are the likeliest fields to carry a credential or someone's data, and
this log is meant to be safe to surface. What a digest still answers is whether two
calls were made with the same arguments and whether a retry returned the same
bytes, which is what a cost or loop investigation actually asks. Keys are sorted
before hashing, so the same arguments hash the same however the dict was built.
`result_bytes` and `result_hash` describe the full redacted output, not its displayed prefix.

`is_error` is passed in rather than derived. The stream carries no error boolean:
a refusal is the PRESENCE of `event.refusal`, so the site that can see that object
computes the flag and the emitter records what it was told.

### The model recorded is the one the session runs on

`slot.model` is the CONFIGURED pin, and it can name a model the session never ran. A
pin this account cannot run is withheld at spawn and the session runs on the backend
default, while the pin is deliberately KEPT -- the composer chip still shows it, and
clearing it would delete an explicit user setting on the evidence of one session's
advertised list. An unpinned slot is the mirror case: its pin is backfilled from the
provider only after the handle exists.

So `session/opened` and `request/configured` read `served_model`, the session fact,
through one helper. It is empty when the backend serves its own default, and the entry
records that emptiness rather than naming a model -- the same rule that omits an
unmeasured token count. `session/opened` is therefore written after the withhold
verdict rather than at acquisition, which is still ahead of every turn entry.

That emptiness has two causes, and the field alone cannot separate them. No tier
resolved anything above the backend's own default, or a tier resolved a concrete
model that never took effect: a model this account cannot run is withheld before it
is sent, and a `set_model` that raises is logged and the session left on the
backend's choice.
Both write a warning to the server log and nothing to the record, so a dispatched
worker running on a model nobody chose looks identical to one deliberately on auto.

`session/opened` therefore also carries `model_requested`, the model the gateway
SELECTED through the slot pin, the crew pin and the resolved default. The value is
bound once at allocation, handed to the provider, and retained with the live slot so
the first turn cannot replace it with a newer config resolution when it claims a
pre-warmed session. It is written whenever that allocation resolved a tier, and its
presence is NOT conditioned on `model`. When this process did not observe the
allocation -- for example, on re-attach -- the field is absent rather than inferred
from the observing turn. Selection is not transmission: the provider withholds a
model this account cannot run rather than sending it, so the field names the choice,
not a message the backend received.

That unconditional rule is the point, because both ways of conditioning it lose
the record. Suppressing it when it DIFFERS from `model` reports an honoured request
as unconfirmed, since the backend serves the spelling it resolved
(`resolve_pin_spelling` at send, `resolve_usable_model` inside `set_model`).
Suppressing it when `model` is KNOWN drops the request whenever a refused pin
leaves the session on a concrete backend default instead of the auto sentinel,
which is ordinary operation -- and the entry is append-only, so that pair is gone
with no recovery. Recording the request outright costs one short string.

So the entry states two facts and infers nothing: `model` is what serves the
session, `model_requested` is what was asked for. Whether the request was APPLIED
is deliberately not a field here. Deriving it would mean either comparing model
spellings, which is what the two failed guards did, or teaching the record writer
to resolve models, which a spelling the registry cannot map defeats anyway. A
consumer that needs the outcome reads the provider's own, and the served id a fold
can trust arrives on `turn/completed`.

The absence of `model_requested` carries its meaning only forward. Entries written
before the field existed have none regardless of what was asked for, so a fold
spanning the upgrade reads an absent field on an older entry as unknown rather
than as "no tier resolved one" -- the reference page states the same boundary beside
the field.

### A closer states only the outcome its site observed

`close_open_tool_calls` defaults to `status: "unknown"`, not `"completed"`. The two
callers know different things. At a tool-group boundary the inference is grounded --
the model went on to produce text, so the tools it was waiting on finished -- and that
caller passes `"completed"` explicitly. At turn end nothing of the kind is known: the
stream may have raised or the process may have died, and a call still open there has an
outcome no site saw. `"unknown"` is the word `repair_interrupted_turn` already writes
for an unmatched `tool/called`, which is the identical claim reached from the file
instead of from memory, so the log has one vocabulary for it.

`is_error` stays absent on an unknown close rather than being taken from the turn's own
failure: a turn that raised says so in its own terminal, and marking the TOOL as errored
would be a second, unobserved claim.

### Writer loss is admitted before appending resumes

Every writer loss creates one per-session pending-loss record: `{dropped_count, dropped_bytes}`.
Retry exhaustion, a hard buffer-ceiling rejection and a permanent refusal all feed the same record,
and so does a refusal on the inline path. `dropped_bytes` is the size hint the producer supplied for
the rejected jobs.

The record carries no reason code, deliberately. The only site that knows a cause cannot separate
the ones that matter: `_permanent` collapses a malformed entry and a well-formed entry refused
because another process owns the log into a single boolean, and it is the second that is a genuine
hole. Marking every loss makes the distinction unnecessary, and a reason a reader cannot trust is
worse than none. What a reader acts on is that facts are missing and how many.

The pending record is outside the bounded ordinary buffer, so memory pressure cannot reject the
account of that same pressure. The writer inserts `write/dropped` at the head of that session's
next claimed batch. If more loss arrives while the marker is queued or retained, the counts and
bytes merge into it. If the marker itself reaches the write-attempt ceiling, its payload is folded
back into the next marker along with ordinary jobs that could not pass it. The session is not
poisoned: a later append retries the marker and can resume once it lands.

The ordering invariant is: **no entry is ever appended after a loss until a marker naming that loss
has been appended.** Jobs emitted before a concurrent loss may already be in flight; every ordinary
job still waiting behind that observation is requeued behind the marker without spending its retry
budget. `seq` remains contiguous because lost jobs never acquired a seq. A reader detects the hole
from `write/dropped`, not from a numeric gap, and can fold later entries normally after admitting the
loss.

### A bounded shutdown clamps the retry backoff instead of waiting it out

A failed append parks its batch in a backoff. Honouring that schedule during shutdown
does not defer the write -- the process is leaving, so nothing retries it later and the
entries are gone, and these are the last entries each session produced.

The backoff is therefore CLAMPED to just inside the drain's deadline, never collapsed.
Collapsing it would spend every remaining attempt in microseconds against a filesystem
that needed a moment, turning a delay into a guaranteed drop at the worst possible
time; clamping gives the filesystem the most time the budget contains and then makes one
attempt. Only what still fails at the deadline is counted in `dropped_writes()`.

The writer's inter-pass pause is interruptible for the same reason. It is computed
before the shutdown is requested, so a thread parked for the length of a backoff would
never see the clamped deadline -- a shutdown sets an event that makes it recompute.

### The input is recorded before what was derived from it

A turn writes `message/received`, then `request/configured`, then `context/composed`,
at one site. The last two describe a request assembled FROM the accepted message, so
writing either first puts a derived fact at a lower seq than its cause -- context
composed for a message the log has not yet admitted arrived. All three still precede
the dispatch gates, so a refused turn shows what was asked.

`context/composed` carries no `step` at this site. It is written before the turn's
first `step/started`, so no model call has been announced yet, and giving it one
would mean either emitting it after that opener -- which drops it below
`message/received`, the very inversion this ordering rule forbids -- or minting a
step before the first call really opened. So the entry stays step-less, and the
join is a READER rule rather than a field: **a `context/composed` with no `step`
belongs to its turn's first model call.** A turn composes its context once, in
front of the call that `step/started` then opens as step 1, so the first call is the
only one a step-less composition can name. A later composition, if one is ever
emitted per call, would carry its own `step`; the step-less form is specifically the
turn-opening one.

The refusal gates keep the front half of this order and drop the derived half. A
turn a gate refuses -- an unauthorized dispatch, a gateway closing, a stop before
dispatch, a superseded replay, or a **blocked or oversized `@prompt` expansion** --
writes `message/received` for the accepted input and then `turn/refused` naming the
reason, and writes NEITHER `request/configured` NOR `context/composed`: those are
derived from a request that a refused turn never assembled. The `@prompt` expansion
gate returns before the request is composed, the same as every other refusal gate,
so it records the same pair rather than nothing -- the accepted input a reader most
needs to see beside the refusal is still there.

### `session/closed` is a teardown, not the end of the file

A forced reset -- the model-switch route called with `skip_running` false -- tears a
session down while a turn is still running. That turn's tool, step and turn closers
are written later, by its own `finally`, in another task, and a steer already inside
its RPC lands later still. So entries belonging to turns that were ALREADY IN FLIGHT
may follow this one, and the entry means "the gateway stopped serving this session for
reason R" rather than "nothing further appears". A reader folds the whole file; it does
not stop here.

Holding the entry until the session went quiet was implemented and then removed,
because nothing in the gateway defines quiescence and each approximation loses the
terminal outright, which is the worse failure -- an absent terminal cannot be told
apart from a crash. A turn is pinned only once `turn/started` is written, so the
authorization await ahead of it is unpinned and a reset landing there would still be
followed by `turn/started` or `turn/refused`. Eviction under `_MAX_LIVE_TURNS` removes
the pin without consuming a held reason, stranding it forever. And a resume that
re-claims the same ACP session id would have to decide whether the predecessor's
teardown still happened.

What the teardown must not do is erase state its live turns still need. The cleanup
drops the config echo baseline and the attempt counts -- what a successor must not
inherit -- and, of the open tool calls, only those whose turn is already gone. Dropping
all of them left a live turn's calls open for the life of the file, because its own
`close_open_tool_calls` then found nothing to close. The settle-once markers a call
leaves behind are swept on that same rule -- only for turns already gone -- so a closed
session leaves neither the open-call registry nor its settle markers behind, while a
turn still running keeps its own markers to suppress its own late duplicate frames.

The cached HANDLE follows the same rule, and with write ownership bound to it the rule
is load-bearing rather than tidy: a forced reset tears the session down mid-turn, so
dropping its handle there releases the unit's ownership while the turn is still
producing entries, and a second gateway's resume then repairs a turn that completes for
real a moment later -- the exact two-outcome record ownership exists to prevent. So the
handle is dropped only when no turn of that session is live; one left behind belongs to
a live turn and the capacity rule reclaims it once that turn is gone.

### A dying turn still records what it produced

Seven recovery handlers -- cancellation, a signed-out CLI, a dead backend process, an
exhausted prompt-busy retry, and three branches of the generic `AcpError` arm --
persist the turn's partial reply straight through `slot.append` rather than
`_flush_segment`, because they purge the chunk rows first and must not broadcast a
segment. They go through one helper, `_persist_partial_reply`, which writes the body to
the log as well, marked `interrupted`, BEFORE the turn's closers run. One helper rather
than the block inline in each handler, for the reason redaction lives at the emitter's
boundary: an eighth handler cannot forget it, and a test asserts that structurally
rather than per branch, since each branch needs its own error class, retry counter and
depth to reach. Without the log copy the transcript holds text the user
watched stream while the record carries a closer asserting the turn ended and nothing
saying what it had produced.

### Why `compaction/applied` may precede the turn whose growth it includes

A deferred verdict settles inside `check_context_usage`, which the runner calls before
the turn's closers are written -- so this entry can carry a lower seq than the
`turn/completed` whose growth its `pct_after` includes. That is not an inversion to
fix, and moving the closers earlier would introduce a real one: the recovery paths
above persist their body AFTER that point, so the turn's closer would then precede its
own output.

The entry's position records when the COMPACTION happened, which genuinely is before
this turn. Only the measurement is later, and the entry already says so without being
read in order: it carries no `turn`, and `freed_pct` goes negative exactly when the
reading includes a later turn's growth. A reader folding on seq is told the truth by
the fields rather than by the position.

### A steer has no entry of its own, and its position is why

The vocabulary carries no steer type, and the reason is ordering rather than access.
The fact is knowable from exactly one place -- the `steering_consumed` echo, the only
positive evidence that a turn received the text, and the only site that knows which
turn did. The delivery cannot write it: its RPC returning proves the bytes left, not
that any turn received them, and a steer still pending when the turn ends is requeued
to a LATER turn, so a delivery-time entry names a turn that never saw it.

But the assistant text the steer INTERRUPTED reaches the log from the handler's
segment cut, which runs when `client.steer()` returns. Under stdin backpressure that
RPC is still in `drain()` when the echo arrives, so an entry written from the echo
takes a LOWER seq than the text it cut, and a fold reads that text as the reply TO the
steer rather than the reply it interrupted. Cutting the segment from the echo instead
only moves the damage: post-steer text arriving in the same window is then flushed
above the steer row in the transcript.

One resolver would have to own both facts before either site could write such an
entry, and no site can name one whose seq it can defend -- which is why the type is
not in the vocabulary at all rather than sitting in it unwritten.

What the cut site CAN prove is recorded. It is cutting the segment because a steer
arrived, so the flush marks that `message/sent` `interrupted` -- a fact needing no
agreement with the echo, which is exactly why it is recordable where a steer entry is
not. Without it an interrupted reply and a finished one are the same line.

The rest is not lost either. The steer's TEXT reaches the log as the `message/received`
of the turn that runs it, which is what the transcript shows too. And the requeue path
keeps its own entry: `message/queued` is written from `chat_delivery`, from the one site
that has SEEN the queue entry and records that entry's own id -- never from a site that
merely expects a requeue, because a pending steer whose stop race is unresolved can be
discarded by a hard kill before its teardown runs, and an append-only entry cannot be
taken back.

## Which try this is: `attempt`

A regenerate and a rewind both rerun a turn at an ordinal the log already used, so
two `turn/started` lines at one ordinal are otherwise identical and a fold cannot
tell a retry from a duplicate write. `(turn, attempt)` is the identity it groups on.

`attempt` is DERIVED by the emitter, not asked of the call sites. It keeps a
per-session map of ordinal to highest attempt opened, and increments when
`on_turn_started` sees an ordinal it has already opened. The alternative -- each
rerun path passing its own count -- was tried and is why the field arrived unused:
`chat_regenerate` and `chat_rewind` are two of several paths that rerun a turn, and
a field only the audited ones supply is a field a reader cannot trust. A positive
`attempt` argument still overrides, for a caller that genuinely knows better.

The increment runs on the WRITER thread, inside the queued job, not on the caller's.
Resume seeding is an earlier job on the same session and the writer executes one
session's jobs in submission order, so deriving inside the job is ordered behind the
seed by construction. Deriving on the caller's thread reads the map while the seed is
still queued, and the first rerun after a resume then writes an attempt the file
already holds -- which is the exact collision the field exists to prevent. The other
way out, making the caller wait on a seeded event, puts a filesystem read in front of
the turn on the event loop.

## The turn's closer is written last

`turn/completed` is the one entry whose POSITION carries meaning: a reader folding
entries in order treats it as the turn's boundary, so anything after it belongs to a
turn the file has already closed. The runner's terminal stream event is therefore NOT
where it is emitted -- that point is still inside the stream loop, and the turn's last
assistant message is flushed after the loop breaks. Emitting there put `turn/completed`
ahead of a `message/sent` belonging to the turn it closes.

The terminal event stashes the payload instead, and the turn's `finally` emits the
three closers in order once every path has flushed: the outputless-tool closer, the
last step's completion, then the turn's own. The `finally` rather than the normal exit,
because several recovery paths return before the terminal event is reached, and a
stash that never runs leaves a turn open in the file forever.

The map is SEEDED from the file when a claim re-attaches to an existing crew log,
because the in-memory map cannot survive the restart that makes a collision
possible: without seeding, a rerun after a restart writes attempt 1 at an ordinal
whose file already has an attempt 1. Seeding reads that session's `turn/started`
entries once, on the writer thread, on the resume path only, and a file it cannot
read leaves the map empty rather than failing the resume. `attempt` is omitted at 1,
which is every turn that was never rerun.

## Every tool call gets its closer

A tool that produces NO OUTPUT still reports a status, and both update parsers emit a
status-only result event for a TERMINAL one -- `completed`, `failed`, `cancelled`,
`refused` (`TERMINAL_TOOL_STATUSES`) -- carrying that status and no output it did not
see. Dropping such a frame is what left the observed outcome unrecorded: the call's
`tool/called` stayed open and the sweep below closed it as `unknown`, which is a
weaker claim than the one the stream had already made.

What the sweep is still for is a call whose terminal frame never arrives at all, or
arrives only as a non-terminal update. Its `tool/called` would otherwise stay open for
the life of the file, and a fold counting open calls would report a turn that never
finished with a tool it had in fact finished with -- the same shape the
interrupted-turn repair exists to fix, which a live turn must not produce.

`close_open_tool_calls()` closes whatever is still open, and it is called at the two
points where the runner already makes this inference for the transcript: the
tool-group-to-text transition, where text after a group means every tool in it is
done, and the turn's terminal event. The closer carries `result_bytes: 0` and no
`result_hash`, because the tool genuinely produced no bytes -- a different claim from
"the payload was not recorded", which is an absent field.

A call settles exactly ONCE, and the guard is in the emitter's lifecycle rather than
at a call site. The two update parsers feed ONE consumer, so either can produce the
terminal frame and both can produce one for the same `tool_call_id`; a duplicate is
therefore ordinary rather than exceptional. `on_tool_completed` records each
`(session, call_id)` it closes in a settled set and writes at most one
`tool/completed` for it: the FIRST terminal frame writes the closer, and a LATER
frame for a call this emitter already settled adds nothing. The absence of an open
`tool/called` record is NOT the same signal -- the first frame pops that record, so a
missing record cannot distinguish "already settled by us" from "never opened". A
frame for a call whose `tool/called` was never seen -- neither open nor settled --
still gets its closer, with empty name and server and no elapsed, because a call the
stream reports finishing is a fact even when its opener was missed. `close_open_tool_calls`
marks the calls it sweeps settled too, so a terminal frame arriving after the sweep
closed a call does not double-write. The settled set is pruned on the same lifecycle as
the open-call registry: a turn's markers are dropped when its live record is released.

Its CAP, though, is its own rather than the module's shared never-evict-a-live-turn
rule, and the reason is the same one that separates the child pins from turn liveness.
Every other map holds state a later event of the SAME turn reads back -- a handle, a
step ordinal, an open call's start time -- so evicting one mid-turn corrupts that turn's
record, and the shared rule lets a store overshoot instead. A settle marker carries only
"a closer for this id is already written", and one accumulates per call COMPLETED where
an open-call record is popped by its own completion, so under the shared rule a turn that
makes more calls than the cap would grow the map for as long as it runs. So this map
trims its OLDEST entries at the cap and COUNTS what it dropped in the log. The frames a
marker suppresses are two parsers reading one terminal frame, so a duplicate arrives
beside its original: the youngest markers are the ones doing the work and the oldest are
the ones worth spending. The residual is bounded and visible -- it takes the cap's worth
of later calls settling before a duplicate arrives, and it reports itself when it
happens.

## Shutdown reports what landed, and names what did not

`drain_for_shutdown()` returns True only when the buffer is empty AND no batch is in
flight. Both, because the writer takes a batch OUT of the buffer before writing it:
an empty buffer alone says nothing about whether those entries reached the file, and
reporting drained there is the worst answer available -- the caller exits believing
the record is complete, and that batch is the last thing each session did.

The inline fallback runs at most ONE pass. Reaching it means the timed wait already
failed, so the writer is wedged rather than slow, and looping would spend an
unbounded part of the exit on a filesystem that has stopped answering.

The fallback also refuses to write while a writer batch is CLAIMED, and so does the
inline path a synchronous caller takes when no event loop is running. `flock` keeps two
appends from interleaving but does not decide which lands first, so a second thread
writing beside a claimed batch can put its entry at a lower seq than one emitted before
it. A log whose seq disagrees with causality is worse than a short one, because a fold
cannot detect it: a short tail is visible, a reordered one reads as fact. A synchronous
caller therefore waits a bounded time for the batch to finish and then hands its job to
the writer rather than racing it.

**The residual:** a batch a wedged writer still holds cannot be finished from the
shutdown thread, and the entries buffered behind it are left alone rather than written
out of order. A False return means exactly that -- some entries did not land and the
log's tail is short by them -- and the warning names the buffered count, how many
sessions are still owed a `write/dropped` marker, and whether a batch was in flight, so
the gap is attributable rather than silent.

The owed-marker count reads both places a session's loss debt lives. It sits in the
pending-loss map while no marker job exists for that session; once one is built the debt
travels INSIDE the job, because building the marker takes the debt out of the map to
serialize it and a failed append hands the job back to the retained batch. A marker
waiting to be retried is therefore owed while the map is empty, and that is a state a
bounded shutdown reaches on its own schedule: the retry budget is spent only if enough
paced attempts fit inside the caller's timeout, which is a property of the host. Counting
the map alone reports nothing owed at exactly the moment a marker is owed.

The drain runs on EVERY gateway mode, not only where a dashboard exists. The dashboard
registers a cleanup hook, but a mode that builds no dashboard app never runs one and the
gateway's hard exit skips `atexit`, so the drain also sits on the exit path itself --
before the log queue is flushed, so its own warning about an incomplete drain still
reaches the log. Calling it twice is a no-op.

## A step is one model call; a call index orders the tools

PR 1 shipped `step` as the ordinal of a TOOL CALL, because no step existed yet.
This change gives the field the meaning the design defines -- one model call -- and
keeps the older quantity under its own name, `call_index`. Both are on a tool
entry and they answer different questions: `step` says which model call issued
this tool call, `call_index` says where it sat among the turn's calls. One model
call can issue several tools at once, so the step alone cannot order them, and the
pair is not redundant.

The boundary is DERIVED, and that is a property of the stream rather than a
choice. The ACP event stream has no per-model-call event -- `EVENT_COMPLETE` is the
turn's terminal, one per turn -- so the only observable transition is a tool group
followed by fresh assistant text, which means the model was called again with the
tools' results. `step/started` fires immediately after `turn/started`, once the turn is
AUTHORIZED, and again at each such transition; `step/completed` fires at the next
transition and, for the turn's last call, beside `EVENT_COMPLETE`. An entry produced when
no step has been announced omits `step` rather than claiming a model call nobody observed.

Deriving the boundary from text-after-tools has a limit that is a property of the
stream, and the format states it rather than pretending otherwise: **a single step
MAY cover consecutive tool-only model calls.** When the model calls tools, is called
again with their results and calls MORE tools, and only speaks at the end -- read,
then edit, then read again, summarising once -- the stream shows one tool group
followed by one run of text, so the only observable transition fires once and the two
model calls collapse into one step. Separating them would take a per-call boundary
REPORTED on the stream -- an attempt id beside each opened call -- which the ACP
surface does not carry today; adding it is an adapter-surface change held for a later
pass rather than landed here. Until then a reader MUST NOT read the step count as a
count of model calls: it is a lower bound. The tools of such a run still order
correctly by `call_index`, which counts every tool call regardless of how the steps
fell, so no tool is lost -- only the model-call boundary between two tool-only calls
is not observable.

Written any earlier, the first step would land on refused turns too. `turn/started` waits
for the permit, shutdown and stop-before-dispatch gates for exactly that reason, and a
`step/started` in front of them would record a model call for a turn the model was never
invoked for -- with no `step/completed` ever following, since the refusal path returns. It
would also charge the gates' own time to the model call.

## Bodies, and the flag

Message bodies live in the log, and the transcript file keeps being written
exactly as before -- a dual write, with the file still authoritative. Nothing reads
the log's copy yet.

Every text field is redacted BY THE EMITTER, with the same two helpers the
transcript store applies and in the same order: `redact_exfiltration_urls`, then
`redact_credentials`. Some sites hand over text that is already clean and some
hand over raw input a person just typed; a rule enforced at this boundary cannot
be forgotten by the next site added. A redaction failure yields the empty string
and never the input, because this module's promise is that `data` carries no
secrets and a body is the one field that could.

`message/chunk` has ONE producer: the overflow split, which cuts a body too large
for a single line into slices whose seqs the following entry cites in `chunks`.

A chunk group is written as ONE batch -- `CrewLog.append_many`, one lock, one write,
one fsync -- with the citing entry last. Entry by entry, a hard kill between the
chunks and the entry naming them leaves the body on disk with nothing pointing at it:
stored, unreachable, and with no record that the message existed. What a torn write
can still leave is chunks with no citing entry, which the repair truncates with a
warning naming the seq range; that costs the one message mid-write, the same residual
as any single entry lost the same way.

The split decision measures the body AND the entry's other fields, on the escaped
form, because both ride on the same line. The envelope headroom is a fixed allowance
for the envelope's own keys, not a slack fund for caller fields: a message carrying
many attachment ids can exceed it on its own, and an entry the decision said would
fit is then refused by the append -- dropping the whole message and counting it,
which is the one outcome the split exists to prevent.

There is NO streaming emitter, and this is a security boundary rather than a
scoping choice. Redaction runs per entry, so a streamed delta can only be redacted
against itself -- and a credential split across two deltas matches neither of them.
Token-by-token streaming makes that split the COMMON case, not an edge one, so the
pieces would land in an append-only file that any reader can concatenate back into
the secret. The full body on `message/sent` carries the same content, redacted once
over text where the credential is still intact and therefore still matchable.

The overflow split is safe for exactly that reason: it slices text that has ALREADY
been redacted whole, so no slice boundary can cut a credential into two unmatched
halves. A test walks a credential across a slice boundary byte by byte and asserts
neither the file nor the reassembled chunks contain it.

Shipping a streaming emitter requires a redactor that holds back a tail at least as
long as the longest pattern it knows and re-examines it against each new delta.
That is not a small change, and the patterns it would have to bound are not all
bounded -- a private key block and a URL are both arbitrarily long -- so the held-back
tail has no safe fixed size. It is out of scope here.

Each slice is MEASURED, not assumed. No character count can be turned into a byte budget
in advance, because `ensure_ascii` escapes per code UNIT: a BMP character costs six bytes
as `\uXXXX`, one outside the BMP costs twelve as a surrogate pair. So a slice starts at
`_CHUNK_TEXT_CHARS` and halves until it actually fits, which terminates because a single
character always does. Getting this wrong loses the whole body rather than cutting it
badly -- a refused chunk aborts the job, so no `message/sent` follows it either.

## What the gateway cannot say

`request/configured` carries no `tools` list. The design asks for one; the backend
serves tool specs with tool search on and the gateway never receives the resolved
set. The only inventory this process holds is the MCP gateway's per-stub
`_served_tool_surfaces`, which is keyed by stub rather than by session and carries
names without spec sizes. An empty list every turn would read as "no tools", which
is false, so the field is absent.

It also carries no `system`. There is no separate system prompt on the ACP path --
the assembled prompt IS the request -- and that prompt changes every turn by
construction, so a digest of it would make a change-only entry fire every turn and
mean nothing. The parameter exists and is tested for a caller that has a real
system prompt; no ACP site supplies one.

`context/composed` reports CHARACTERS as measured and tokens as an ESTIMATE, at
four characters per token, the same figure `dashboard/handlers/usage.py` uses. The
entry says so in `tokens_estimated`. The only exact tokenizer available is the
wrong one for the served model, and a fabricated exact count would be worse than
an admitted estimate. Three blocks the design names -- steering, tool specs and
injected crew log context -- have no opening marker for `split_blocks` to classify, so
their characters land in its unclassified bucket, which this entry reports as a
single `other` source. Three zeroed sources would claim a measurement nobody took.

## The resolver: which unit is this slot's work landing in

Several sites that produce facts about a session do not hold an ACP client. `kiro_crew.crew_log.resolve`
is the one place that gap is closed, and its contract is narrower than it looks.

A slot owns exactly one ACP session id **at a time**, not for its whole life. A plain resume
replays the persisted id into `session/load`, but a reset, an agent/model/effort switch, a
compaction that recycles the session, and a provider swap all tear the session down and the
successor cold-starts a new id. So the resolver answers "which unit is this session's work landing in
*now*", valid at the moment it is asked. That is exactly what an emit site needs, because every
entry records what was observed at that site when it was observed. It is NOT enough to reconstruct
which unit some earlier fact went to, and nothing uses it that way.

The one lineage edge this log DOES carry is the `session_create` one, and it is
written from the side that escapes the problem above. A created session's
`session/opened` carries `parent {slot, sid?}`: the creator's key is stamped on
the slot at mint (`_created_by`, `session_create`'s own attribution), so it is
settled before the child's first turn, and the creator's ACP session id is
FROZEN at that same mint (`_created_by_sid`), read off the live caller handle
`session_create` just authorized -- NOT re-read at the child's first turn. A
creator slot can be closed and replaced between mint and that turn, and a
replacement is a distinct handle with its own session id; reading the id live at
emit would then cite the replacement's crew log and corrupt this child's immutable
`session/opened` lineage with no recovery. Freezing at mint captures the id that
was live when the child was made. Both halves are used only when the slot carries
the process-local witness `session_create` sets at mint (`_lineage_minted`,
never persisted): `_created_by` is also restored from transcript metadata for the
ownership boundary, and that file is editable by an agent's file tools, so a
restored value must never become the gateway-authored lineage this fenced log
exists to protect -- a child whose gateway restarted between mint and its first
turn writes no `parent`. A
person's own tab and a fork carry no `parent`, so a fold distinguishes "nobody
created this" from "creator unknown" only for sessions minted in the live process. The tree of sessions is therefore a fold
over each session crew log's `session/opened`, keyed by SLOT (the creator's `sid`
changes when its slot recycles, so it is a citation of the unit that was live,
not the tree key).

The resolver has one level, and it is a synchronous registry read: `unit_for_session_key(sessions, key)` asks the
session registry for that key's provider and reads the id off it. Every production caller holds a
session KEY rather than a slot -- the subagent manager is handed `info.parent_session_key`, and the
background helpers are handed the key the site was called with -- so a key is the only input the
resolver needs.

An earlier draft had a second, slot-keyed level that read the slot's live `_acp_client` first and
fell back to the registry. It is not shipped, and the reason is worth recording: the registry's
provider IS the object the runner reads its own `_crew_log_sid` from, so it answers the same id during
a turn as between turns. The extra level asked no question the one level cannot, and no caller ever
reached for it.

The persisted `SessionMap` is deliberately not consulted. Its `get` repairs or removes an entry it
finds stale, which makes describing a session mutate it, and it answers only for ids that reached
disk -- missing exactly the live session being asked about. There is no reverse function either:
`find_key_by_sid` is a scan over persisted ids and cannot answer for a live one, so it is not the
cheap lookup a reverse direction would have to be.

`unit_for_session_key` allows one retry, under a premise that makes it a lookup rather than a
guess: a key containing no colon cannot already be namespaced (`dashboard:`, `slack:`, `subagent:`
all carry one), so a bare slot name is retried in its dashboard form. A key that already carries a
namespace is never rewritten -- that is how `slack:<ts>` would become the nonexistent
`dashboard:slack:<ts>`.

Unknown is an answer. Every function returns the empty string when the key has no live ACP session,
and the emitter's own no-op guard turns that into "do not write". A crew log that omits a fact is
behind; one that files a fact under the wrong session is wrong, and nothing downstream can tell.

### Approvals were never a resolver problem

The previous revision of this document said approvals had no emitter because "the approval
coordinator carries a slot key, not a session id". That is true of `ApprovalCoordinator` and false
of the site that actually raises the prompt: the permission-request arm lives inside `_run_chat`,
where `_crew_log_sid` and `_crew_log_turn_no` have been in scope since the turn began. No resolver is
involved.

The request is recorded ONE STATEMENT before the `try` whose `finally` records the decision, and
deliberately not at the future's registration further up. Everything between those two points is
cancellable: the Slack mirror awaits a network post, and its `except Exception` cannot catch the
CancelledError that slot deletion raises. A request written at registration could therefore escape
that `try` entirely and stand forever undecided, in a file nothing rewrites. Written where it is, the
pair is bound by control flow -- either both halves land or neither does. The cost is that a prompt
cancelled during its Slack delivery goes unrecorded, which is a fact the log is missing rather than a
pair it gets wrong.

It is still recorded before the decision on every surviving path, including the delivery failure that
auto-decides the approval: that branch only resolves the future, and nothing writes a decision until
the `finally`. And that `finally` is the single point every exit converges on -- the human's answer,
the window expiring, the no-budget decline, a failed Slack delivery, and a cancelled wait. One
request, one decision, whichever path won.

`by` is written only for a decision the host made, because that is the one attribution the site can
prove: `_host_deny_cause` is set exactly by the gateway's own auto-declines. A decision that arrived
through the future was made by a person at the dashboard or in Slack and the runner cannot see
which, so it names nobody rather than asserting `user`. The host's reason code rides in its own
`cause` field instead of replacing `decision`, so a reader still learns what was decided without
knowing the reason vocabulary.

### A child is pinned when accepted and written when it starts

Two different moments, and conflating them produces two different defects.

The **asking turn** is knowable only at acceptance: a spawn arrives as a tool call inside the
parent's turn, while the child's own entries are produced long afterwards, usually while the parent
is on a different turn. So `(session id, turn)` is pinned there and every later entry about that
child reads it back.

The **entry** may not be written there, because acceptance is not a start. Registration is followed
by the spawn approval gate, and a decline returns through the finalize claim and the announce
without ever reaching `_run` -- so an entry written at acceptance would be an opener nothing closes,
for a run that never existed. `_log_spawned` is the codebase's own "this run is really starting"
funnel: every auto-approve branch reaches it, and the approval branch reaches it only after a human
said yes. That is where `subagent/spawned` is written, from the pin.

The pin therefore carries an `opened` flag, which makes the ordering rule structural rather than a
convention. Until the run starts the pin is invisible: a steer reads nothing, and a terminal report
closes nothing while still dropping the pin. A declined spawn leaves no trace at all rather than an
outcome with no cause.

Pinning is idempotent, and that is load-bearing twice over. A member held behind the stagger or
concurrency gate is accepted, returned as queued, and re-enters the spawn path under the same id
when the queue drains -- one dispatch, two passes -- and a second `_log_spawned` cannot produce a
second opener.

One caller cannot use the live reading at all. A queued **follow-up** is dispatched by its watcher
after the run it continues has finished, so no turn is asking at that moment and the parent may be
on an unrelated one. Its asking ordinal is pinned where the follow-up was REQUESTED -- `spawn_steer`
is itself a tool call inside the asking turn -- and carried to the dispatch on the run record, the
same way `_preassigned_id` and the inherited context groups already ride that call. Without it the
continuation would be filed under a turn that did not ask for it.

Several follow-ups queued across several turns are delivered as ONE continuation, so that entry can
name only one turn, and the pin it carries is the most recent ask's. That is a choice rather than a
measurement, and it is the honest one available: the dispatch is a single child, the last ask is the
one it was waiting on, and each individual ask is separately recorded as its own
`subagent/steered` at the turn that made it.

Some dispatches have no asking turn at all -- a slash command, a cron, a hook -- and those record
the child with `turn` ABSENT rather than zero. Turns are numbered from one, so a literal `0` would
name a turn that never existed. The child is still recorded, because it is a real child of that
session and dropping it to keep a field populated would be the worse trade.

The map holding the pins is bounded rather than tied to turn liveness, because its entries
deliberately outlive the turn that created them, and a pin is released by the child's own terminal
entry. A pin still held while its neighbours have gone therefore belongs to a child that never
reported one -- a run lost to a crash, a member cancelled before it started -- and those collect at
the old end, which is what would make the cap reachable by accumulation across long uptime rather
than only by that many children genuinely running at once. So the cap asks the liveness probe over
a bounded window of the oldest pins and drops a finished child's pin in preference: the entries that
pin could still carry are never coming, so dropping it costs nothing. The window is bounded because
answering takes a call into the subagent side per pin, and the probe is asked with the module's lock
released, since holding a non-reentrant lock across a foreign call is how a deadlock is built.

Only when every candidate is still running does the oldest go, and then the drop is COUNTED in
`lost_child_origins()` and named in the log once. That child's opener or outcome will be absent, and
an absence a reader cannot tell apart from a child that never existed is the one loss this module
refuses to allow silently. Nothing is WRITTEN for it: no declared type describes a lost pin, and
adding one would be a shape change made to record a bug rather than a fact of the session. The
residual is bounded and visible: it takes the cap's worth of children running at once, and it
reports itself when it happens. Admission is NOT gated on the cap -- this is a default-off log, and
a log that refuses a real spawn to protect its own bookkeeping has become the more expensive
failure.

A cap on the NUMBER of pins bounds memory only when each pin's own fields are bounded, and the
session id a pin carries is authored by the provider. So a pin whose session id is longer than the
declared maximum is refused at the point of retention and counted the same way, rather than stored
or shortened: an identity that has been cut down names a different unit or none at all, so a
truncated copy would file that child's entries against the wrong crew log. The maximum sits far above
any id this codebase produces, so it rejects nothing legitimate and exists only so a broken or
hostile provider cannot make the count cap meaningless.
### `subagent/spawned` carries no `ref`, and that is not a deferral

The schema describes a `ref` into the child's log, and a child that had a crew log would deserve one.
No subagent code path opens one: the only site that creates a session's crew log is the dashboard turn
path, and a subagent run does not go through it. A `ref` written now would cite a file that does not
exist, which a reader cannot distinguish from one that was deleted. It becomes writable, unchanged,
the day subagent sessions get crew logs of their own.

`subagent/completed` likewise carries no `tokens` and no `credits`, and the absence is the record.
The schema has both fields; nothing in the subagent runtime measures either. A run's record carries
elapsed time and peak resource use, and the child's spend is never reported back to the parent.
Zeros there would present the absence of a measurement as a measurement of zero.

### A stopped child is not a completion and not a failure

The runtime's canonical outcome is three-way -- `completed`, `failed`, `stopped` -- and its own
docstring warns that the legacy `error ? failed : completed` idiom misreports a user-stopped agent
as completed. The schema offers two closers. So `completed` closes as a completion, and `failed` and
`stopped` both close through `subagent/failed` carrying which one it was in an additive `outcome`
field. That keeps the two distinguishable without renaming a frozen type and without leaving the
`subagent/spawned` entry open forever.

### Background spend is recorded where it is already being counted

Both background entry points -- the one-liner and the shared-session context manager -- already
snapshot the turn's `TurnUsage` and its wall clock in their teardown, behind the same
`usage_has_billing` gate the usage store uses. The crew log entry is written from that exact point, so
a background call appears in a session's log precisely when it appears in the account's bill and the
two cannot disagree about whether it happened.

This corrects the earlier claim that these calls could not be measured. What they lacked was not a
measurement but an OWNER: both helpers knew what the call cost and neither knew which session it was
for. Both now take a kind and an owning session key, and write nothing unless given both -- because
a background call is shared infrastructure by default. Titling is charged to the session it titles;
a tip, a folder icon or a cron label is charged to nobody, and picking a session for one of those
would put someone else's cost in a user's log. Three kinds are emitted today: `title`, `summary`,
`memory_consolidation`.

`background/completed` names no turn. The call runs after a turn ends, on a separate session, and
naming the turn that happened to be last would attribute the cost to work that did not cause it.

The OWNER is resolved before the call, not in the teardown that writes the entry, and the reason is
the resolver's own contract: it answers which unit a slot's work is landing in *now*. A slot can be
reset, switched or compacted while a model call is in flight, and the successor cold-starts a new ACP
session id -- so a teardown-time lookup would hand this call's spend to a session that never incurred
it, silently, in a file nothing rewrites. Reading it up front pins the unit that was current when the
work was ordered. This is the general rule for every future emit site too: the resolver answers a
question about the present, so anything that outlives the moment it was asked must carry the answer
rather than ask again.

**What this misses, and why the alternative is worse.** A background call charged to a session whose
ACP session is already GONE -- idle-expired, reset, or a consolidation scheduled long after the tab
closed -- resolves to no unit, so its spend reaches the usage store and never reaches the log. The
crew log FILE still exists on disk; what is missing is any live thing that names its id, and finding
one would take a persisted key-to-id index this change deliberately does not add.

Resolving later does not fix it and makes something worse. Later is strictly no more likely to find a
live session, and in the one interleaving where it finds one that an earlier read would have missed --
the owner starting a fresh session WHILE this call runs -- that session was created after the work was
ordered, so the entry would name a unit that did not incur the cost. An omission a reader can see is
the smaller failure than a confident misattribution it cannot.

### The plan records two states because the stream carries two

`plan/updated` is written inside `slot.set_todo`'s own change gate, which is what keeps a turn that
echoes an identical snapshot on several tool results from writing the same list repeatedly.

Each task arrives with a plain `completed` boolean and no in-progress state, as the slot's own
snapshot code documents, so `state` is `done` or `open`. A three-state vocabulary would read better
and would be invented here. An update is a WHOLE list, not a delta, so the entry is the list as of
this update and a reader diffs consecutive entries. An event carrying no task list writes nothing --
that is absent data, not a plan of zero tasks -- while an event carrying an empty list is a cleared
plan and is recorded as one. The entry is `ignorable`: the agent overwrites its plan freely and
nothing later in the file depends on any single update having been read.

## Removed types, and why each is gone

Nine types the design named are gone from the session vocabulary: `session/seeded`,
`message/steered`, `tool/searched`, `tool/loaded`, `skill/searched`, `skill/loaded`,
`summary/written`, `remote/placed`, `remote/lost`, along with `digest` as a
`background/completed.kind`. Each either has no site that could honestly produce it --
tool search runs inside the backend CLI, so its query and loaded specs never enter this
process; the skills loader takes only a `project_dir`; a steer's consumption is provable
only from an echo that races the entry it would have to be ordered against -- or would
record a second time a fact another entry already carries. Removing them keeps the
vocabulary a statement about what the log contains rather than a wish list, which is what
makes a reader's `known=` set worth declaring.

Any of them may come back when a real source exists: while the format is pre-release
(`crew-log-core.md` section 5) that is an ordinary change, and after the freeze point it is
an additive one, since re-adding a type is exactly the case the `ignorable` marker and the
unknown-type refusal already handle.

With the families this revision emits, nothing the vocabulary still declares is left
waiting for a site: `approval/*`, `background/completed`, `subagent/*` and `plan/updated`
were the four the previous revision grouped as one resolver-shaped problem, and they are
written here.

Every compaction reaches the crew log on exactly one of those two paths. A reading kiro-cli
reset to unknown mid-turn defers its verdict to the next confirmed reading, and that
settlement is the first measurement the compaction has; recording it there is what keeps
the hardest-to-measure compactions from being the ones missing from the log. On that path
`pct_after` can exceed `pct_before`, because a deferred reading includes a later turn's
growth -- `freed` is then negative rather than absent, which says plainly that the pair is
not a clean before/after.

### Actor is structural, never read off the message

A turn's `actor` is a claim a reader takes as fact, so it comes only from a source the user
cannot write:

* `autonudge` -- `_directive_self_wake`, set by `_fire_dashboard_nudge` alone.
* `cron`, `subagent` -- the consumed queue entry's enqueue-time `kind`
  (`CRON_NOTIFICATION_KIND`, `SUBAGENT_COMPLETION_KIND`), stamped by the producer at
  `queue_append`; or, for an injector that dispatches `_run_chat` itself rather than
  queueing, the `_turn_actor` argument that injector passes.
* `app` -- the app-token auth middleware's `request.app`, stamped from the token that
  authenticated the request rather than read from its body, so a person cannot write it.
* `user` -- **no dispatch claimed the turn.** This is the fallback for every other entry
  point, not a positive identification.

The message text is never consulted. `CRON_NOTIFY_PREFIX` and
`SUBAGENT_COMPLETION_PREFIXES` are strings a user can type into the composer, so deriving
the actor from them let a user attribute their own turn to automation. This is the same
source, and the same reason, as `chat_utils.is_system_injection_item`, whose content
fallback was removed to close that gap. A turn whose actor is not in `ACTORS` records
`other` rather than a guess.

Every dispatch that reaches `_run_chat` is listed, because the default is what made
the finding: an unnamed site records `user`.

| dispatch site | what produces the message | actor |
|---|---|---|
| `chat_handlers._api_chat` | dashboard composer, or an app backend under its own token | `user`, or `app` when the request carries one |
| `slack/handler` linked-thread router | a channel message | `user` |
| `openai_compat.chat_completions` | API request body | `user` |
| `chat_regenerate` (regenerate, edit-resend) | the user's own message, re-run | `user` |
| `chat_rewind` | the user's own message, rewound | `user` |
| queue drain (`_finish_queue_cycle`) | the consumed entry's enqueue-time `kind` | `cron`, `subagent`, else `user` |
| queue drain, a synthetic recovery | the original turn's actor, stamped at requeue (`meta.turnActor`) | whatever that turn's was |
| `handlers/messaging` cron branch | a cron notification | `cron` |
| `slack/gateway` cron branch | a cron notification | `cron` |
| `slack/gateway._fire_dashboard_nudge` | a nudge/monitor wake (`_directive_self_wake`) | `autonudge` |
| `slack/gateway` sub-agent injection | a sub-agent completion | `subagent` |
| `slack/gateway._retrigger_recovery` | re-injected sub-agent completions | `subagent` |
| `chat_runner` synthesis dispatch | the sub-agent synthesis prompt | `subagent` |
| `issue_radar.crew_runtime` | a crew-composed prompt | `crew` |
| `handlers/taskrunner` (plan, result) | a task-runner summary | `gateway` |
| `chat_orchestrator` stage loop | orchestrator stage context | `gateway` |

One shared helper passes no actor on purpose: `spec_builder.runtime.enqueue_or_run_prompt`
takes both the message and its origin as parameters, so its actor is its CALLER's fact and
hardcoding one here would record a guess; it inherits `user` until the caller supplies one,
which is the same default every unaudited path has. `_api_chat` serves an app backend as
well as a person and can tell them apart without asking its caller, because the auth
middleware has already stamped the token's app on the request -- so it names the actor
itself. A site that holds the fact and stays silent does not record a missing detail; the
fallback fills it in, and the log states a person typed a message no person touched.

`approval/requested` and `approval/decided` ARE emitted, and no plumbing was needed for
them -- see "Approvals were never a resolver problem" above. `ApprovalCoordinator` does
carry only a slot key, but it is not the site that raises the prompt: that arm lives inside
`_run_chat`, where the session id and the turn ordinal have been in scope since the turn
began.

## What is deliberately not recorded

Tool arguments and tool results are recorded as a DIGEST and a byte count, never as
bytes; message bodies ARE recorded, redacted, and split when they exceed the line cap.
A tool call is identified by
its `tool_call_id` inside `data`, not by `ref`: a `Ref` cites lines of another crew log, and
an ACP frame is not a crew log unit. That id is the join key the transcript already uses.

A tool's `name` and `server` carry ONLY the trusted `_meta.kiro` identity, so both are
empty when the backend supplies none, and the call id still joins the entry to the
transcript. Neither `title` nor `wire_title` is used as a fallback: for a shell tool those
are model-authored, and a log whose record of what ran can be written by the model is
worth less than one that admits it does not know.

`compaction/applied` carries **percentages, not token counts**: that boundary measures
`provider.context_usage_pct()`, never raw tokens. The session's STARTING model is not a
`model/selected` entry: it resolves before the session id exists, so it rides on
`session/opened`, while the model a turn served rides on `turn/completed`.

A turn that ends WITHOUT its terminal event still gets its closers, written by the
turn's `finally` through `on_turn_failed`: `turn/completed` with `stop_reason:
"failed"` and, when an exception was caught, `error` naming its CLASS -- never its
message, which can carry a path or a credential. `tokens` and `credits` are ABSENT
rather than zeroed, because no usage event arrived and a turn that streamed real
text must not carry a durable line claiming it cost nothing; that absence is also
what tells a synthesized closer from a provider-reported one. `duration_ms` is
measured from the turn's own start.

Leaving the `turn/started` open instead would be the wrong record, not the honest
one. This process OBSERVED the end -- the stream raised, or a recovery path
returned before the terminal event -- while an open start says the opposite, that
the writer died mid-turn. And nothing here would ever correct it: the
interrupted-turn repair is opt-in and only a RESUME asks for it, so the turn would
sit open until some later resume closed it as an interruption that never happened,
stamped at the last real entry's time. A turn left open is therefore only ever the
record of a writer that is GONE, which is exactly what the repair is for.

Which is exactly why `turn/started` is written only once the turn is AUTHORIZED -- after the
permit check, the shutdown check and the stop-before-dispatch check, immediately before the
stream opens. A start means the turn ran. Written any earlier, every refused dispatch would
leave a start with no completion, indistinguishable from a turn that died mid-flight, and
the interrupted-turn repair would later close it as though it had. Each refusing gate
records its own `turn/refused` naming which gate refused it (`not_authorized`,
`gateway_closing`, `stopped_before_dispatch`), so a refusal is visible without being
disguised as a run. The emit adds no suspension point between the stop-generation read and
the stream's turn registration -- it hands the write to its own thread and returns -- so the
atomic span those gates depend on is unchanged.

A recovery re-entry writes its own pair rather than folding into the original turn, and
`depth` tells them apart. It is a second turn of the SAME actor: a stall or watchdog
re-entry does not change who caused the work, so the requeue stamps the original turn's
actor on the entry (`meta.turnActor`) and the drain reads it back. Without that the
recovery would arrive with a fresh queue id and a kind that names no producer, and fall
back to `user` -- recording an autonudge's or a cron's retry as a person's message.
Tombstones, projections, a reader, and any UI are out of scope.

## Failure policy

Every entry point is fail-soft and returns `None`. A storage error is caught, reported
once per process at warning level, and demoted to debug afterwards so a broken crew log
cannot flood the log. This matters at three sites: the approval decision resolves on the
websocket handler task, so an exception there would break the click handler and hang the
waiting turn; `_default_session_model` runs inside `asyncio.to_thread` behind a
swallow-all `except`; and `_fallback_swap_for_turn` holds `slot._model_pick_lock`. The
emitter is called from the callers of the last two, never inside them.

### Retention: a failed append is retried, a refused one is not

Fail-soft governs what reaches the CALLER. It does not license losing the entry, and an
earlier draft of the write-behind did exactly that: the drain pass cleared the buffer before
it wrote, and the per-job `except` reported the exception and moved on, so one `ENOSPC` took
that entry out of the log for good. In an append-only file that is unrecoverable and
invisible, which is the failure this module exists to prevent.

So a failed append is RETAINED. The batch -- the failed entry and every entry behind it --
goes back to the FRONT of its session's bucket, and the writer retries it after
`_RETRY_BACKOFF_SECONDS`, doubling to `_RETRY_BACKOFF_MAX_SECONDS`. The front, and the whole
remainder rather than just the entry that failed, because the entries behind it belong AFTER
it in the file: writing them while it waits would put that log on disk in an order that never
happened, which a fold reads as fact and cannot detect. Producers append to the same bucket,
so a later write queues behind the retained batch by construction, and the inline fast path
is off for a session that owes anything -- a synchronous caller queues too rather than
overtaking its own earlier entry. The backoff is per session, so one wedged crew log paces only
itself.

It is BOUNDED, and that bound is the honest half. Entries live in memory until they are
written, so a filesystem that never answers would hold them forever and turn every bounded
caller into a timeout -- `flush()` and `drain_for_shutdown()` would both report failure while
the writer retried a batch nothing could ever accept. Past `_MAX_WRITE_ATTEMPTS` consecutive
failed passes the batch is dropped, counted in `dropped_writes()`, and named once per session
at warning level, with the recovery its own single line when a write for that session lands
again. A wedged disk therefore becomes a reported loss, never a hang. Attempts are counted
CONSECUTIVELY and any append that lands resets them, which is also what makes the retry
terminate: a reset costs a real append, so the buffer strictly shrinks between resets.

A REFUSAL is not retried at all. A `CrewLogError` is the storage layer declining the entry
against the format -- an over-cap line, an unowned type -- decided before a byte is written, so
the file is byte-identical and the same entry is declined identically every time. Retaining
one would spend the whole attempt budget on a verdict that cannot change and hold that
session's whole log behind an entry that can never land, so it is dropped and counted at
once, and the pass continues past it: the entries behind a refused one are not waiting for
anything.

`already_owned` is a refusal on the same terms, and it is the one that is not about the
entry. Another PROCESS holds that unit's write ownership, so this gateway appends nothing --
see "Write ownership" in the crew-log core spec. Ownership lasts as long as the owning process,
which no attempt budget outlasts, so retrying is the same waste with a worse consequence:
every later entry for that session would queue behind one waiting for a process to exit. The
degradation is therefore that a second gateway claiming a live session LOSES ITS OWN ENTRIES,
counted in `dropped_writes()` and named once per session, while the file keeps one writer's
account of the turn. That is the trade being made deliberately. The alternative -- writing
into a log another process is repairing and appending to -- produces a turn with two
outcomes, which no reader can resolve and no later pass can undo, and it costs the owner's
history rather than the intruder's.

Shutdown does not collapse the backoff. Burning the budget in no time at all against a
filesystem that only needed a moment converts a delay into a guaranteed loss, at the one
point where the entries matter most -- the last thing each session did. `drain_for_shutdown`'s
own timeout is what bounds that path instead.

## Storage runs off the event loop

The storage call takes the unit's lock, reads a bounded tail to assign `seq`, and `fsync`s
the appended line -- a `flock` that waits and a kernel `fsync`, once per tool frame and once
per turn. Every call site is inside the async chat path, so running it inline blocked the
one loop that also drives the liveness heartbeat: `no-blocking-call-on-event-loop`, not a
latency preference.

So an entry point does only what must be measured where it is called, and hands the
storage call to `executors.crew_log_executor()` -- a pool of exactly ONE worker, because the
order entries reach a unit's file is part of the format. The call sites stay synchronous
and gain no suspension point; in particular `_compaction_gate_decision` and
`_settle_compact_cooldown` are synchronous methods called from async code, which an
`await`-based emitter could not have served without changing their signatures.

Which turn an entry belongs to is CARRIED IN the entry, not looked up. Every entry that
belongs to a turn names it in `data.turn` -- the runner's own ordinal, which the call site
already holds -- and a tool call also carries `data.step`, its position among that turn's
calls, reused by the completion that closes it. So an entry is self-describing the moment
it is built: nothing has to be cached, read back from a line written earlier, or kept
alive across the queue, and no eviction or loss of in-process state can make an entry name
the WRONG turn. `session/opened`, `session/closed` and `compaction/applied` carry no
`turn`, and the absence is the record: the first two are not turn-scoped, and a
compaction's deferred verdict can settle turns after the compaction it measures, so any
ordinal stamped on it would name a turn that did not cause it.

The envelope's `thread` field is left UNSET on every session entry. `thread` points at
another LINE's `seq`, which is only knowable for a unit whose anchor line is written
before the entries that cite it; that is the crew's log's shape, not this one's.

## Reconnect is not resume

Open handles are cached with a bound, so a handle can go missing and the next emit
reopens the crew log. That reopen is a RECONNECT and it never asks for the interrupted-turn
repair: a handle absent from the cache says nothing about the writer's health, and the
turn is typically still running, so closing it would append a `turn/completed
{interrupted}` mid-turn and then keep writing past it.

The one caller that DOES repair is the resume: `on_session_opened(..., resumed=True)`,
which means this claim re-attached to a conversation a different gateway process was
writing. A turn left open in that file belongs to a writer that is gone, so closing it
records what happened. A warm reuse inside this process passes `resumed=False` and repairs
nothing.

A create that SUPERSEDES a crew log only NAMES it. The entry records which crew log this slot was
writing before, and the emitter opens no crew log for writing but this session's own, so no outcome
for another unit is ever authored here. The candidate is verified before it is named: the emitter
reads that crew log's own header -- written once at create, never rewritten -- and records the edge
only when the header's slot equals this slot, because the id arrives from a source that can name a
crew log the slot never wrote -- a persisted mapping entry can be stale or recycled -- and comparing
that source against itself would prove nothing. The
open is a read; `CrewLog.open` claims write ownership only when asked to repair, and nothing here
asks. A candidate whose header cannot be read gets no edge, since unverifiable is not verified.
Closing a superseded crew log's dangling turn and tool calls still needs a deferral that can resume
once that unit's outstanding writes settle rather than being decided once, which the edge neither
needs nor has; it is tracked with its reproductions as #12148.

`resumed=True` is a BELIEF about a writer this process cannot see, and two things check it,
because they see different populations. A live turn of OUR OWN contradicts the flag directly
-- the writer it claims is gone is us -- so `on_session_opened` reads the live-turn record
before the claim clears it and degrades to a reconnect when one is running, saying so at
warning level. That is the case a cache eviction produces, and no kernel lock can see it:
both handles belong to this process. A writer in ANOTHER process is caught one layer down
instead, by the write ownership the repair takes as an append, and this claim then writes
nothing at all rather than a repair.

A turn in flight OWNS a record, from `turn/started` to `turn/completed`, and that record
holds its step counter. One record rather than a map per concern, because two maps can be
evicted separately: losing a live turn's step counter restarts the numbering so two of its
calls claim one ordinal, and an earlier design that pinned the handle but kept the ordinals
elsewhere left exactly that gap once the pin map itself shed its oldest entry under
pressure. The record is released by an event of the turn's own -- `turn/completed`, a
`turn/refused`, or its session closing -- and by nothing else.

So the live-turn map is not trimmed by pressure at all. It is bounded by the number of
turns actually running, and each of those releases itself. The handle cache and the pending
tool calls still have caps, and they skip any entry belonging to a live turn, which is why
they may sit above their cap while many turns run at once. `_MAX_LIVE_TURNS` is a leak
alarm rather than a cache policy: reaching it means turns are ending without a terminal
event, so the oldest records are shed and the leak is reported at error level, because past
that point unbounded growth is the worse failure.

The writer holds two rules at once, and neither may be traded for the other.

A lifecycle record is never dropped while the process is healthy, at any depth of
backlog. A hole in an append-only log is permanent and silent: a reader cannot tell a turn that
never completed from one whose completion was discarded, which is precisely the distinction this
crew log exists to record, and several subsystems read it to decide what happened. A crash-shaped
loss is different in kind -- the repair names and closes what a kill left behind -- but a discard
chosen while nothing is wrong has nothing that can recover it and no reader that can detect it.
So there is no cap past which an entry is thrown away, and an entry is given up on only when the
filesystem refuses it or keeps refusing to take it -- see "Retention" below.

The cost is stated rather than hidden: a filesystem that stops answering grows this buffer
without limit, and if that reaches the point of killing the process then every session's
unwritten entries go with it, uncounted. That is worse in the tail than shedding one session's
newest entries would be. It is accepted because a ceiling only makes the hole less likely while
guaranteeing that it happens, and because a filesystem this stuck is a failure the log cannot be
available across in any case. What the design owes instead is VISIBILITY, so an operator sees a
cause rather than a quiet gap: `buffered_writes()` and `peak_buffered_writes()` make the backlog
readable, crossing `_PENDING_HIGH_WATER` is reported once, and a write in flight past
`_WRITE_STALL_SECS` is named -- which is the only outward sign a hung write has, since it never
returns to advance the attempt counter that bounds every other failure.

The event loop is never BLOCKED. A producer appends its entry to an in-memory buffer and
returns -- it never writes, and never waits for the writer. So a filesystem that has stopped
keeping up costs memory rather than turn latency. An earlier design let a saturated producer
wait for queue room and, past a deadline, append for itself; both of those put a turn behind
the disk, which is the thing this path exists to prevent.

The buffer is keyed per session, because each session is a separate file and one slow crew log
must not reorder another's entries.

What bucketing does NOT buy is latency isolation, and the distinction matters enough to state:
ONE worker drains every session, so a write that hangs holds up every other session's entries
until it clears or its batch is given up on. Order and content survive -- a held session's
entries stay in its own bucket, in order, and land when the wedge clears -- but a reader
watching a healthy session sees nothing arrive while an unrelated one is stuck. A worker per
session would trade that for concurrent appends to one file from several threads, which
`flock` serializes without ordering, so the cure would cost the seq-follows-causality property
this log is for. The stall is bounded by the same retry budget and per-session ceiling as any
other failure.

**A write that HANGS is the one failure no counter bounds, and nothing is shed for it.** Every
loss described above is bounded by ATTEMPTS: an append raises, its batch is retained, and a
fixed number of failed passes gives up on it. A write that never returns and never raises
reaches none of that machinery, because the counter only moves when a call comes back.
Producers meanwhile keep appending, so the buffer grows for as long as they do. From the
producer side a filesystem that never answers is indistinguishable from a slow one, and the
second must not be punished -- but the answer is NOT a ceiling. There is no per-session cap past
which an entry is discarded, for the reason stated above: a hole chosen while the process is
healthy reads exactly like a fact that never occurred, and nothing can recover or detect it.

So the cost is stated rather than absorbed. A filesystem that stops answering grows this buffer
until memory runs out, and if that kills the process then every session's unwritten entries go
with it, uncounted -- worse in the tail than shedding one session's newest entries, and accepted
because a ceiling only makes the hole less likely while guaranteeing that it happens. What the
design owes instead is VISIBILITY, which is the only thing standing between a hung write and a
silently growing process.

That is why the writer publishes what it is working on and how long it has been at it. A job
past `_WRITE_STALL_SECS` is reported once as a stall, by a PRODUCER -- the thread that would
otherwise notice is the one blocked in the call, so `_submit` checks before it hands over each
entry, which names the stall while the backlog behind it is still growing. A stalled job is not
a failed one: it may still land, and its retry budget has not moved because nothing raised.
Together with `buffered_writes()` and the high-water report, that line is the whole account a
hung write gives of itself.

One worker drains it in batches. It pauses `_BATCH_DEADLINE_SECONDS` before each pass, so a
turn's burst of entries becomes one pass rather than a wake per entry; the deadline is fixed
rather than adaptive so the worst case a producer can impose on the file is a constant.

With no event loop running the write happens inline on the calling thread. That is not the
fallback described above: there is no loop to protect, the caller is a thread that asked for
this, and it is what makes a synchronous caller and the test suite deterministic. `flush()`
exists for a caller that must read the file it just wrote; the turn path never calls it.

Because entries live in memory until the writer takes them, shutdown needs a quiescence
barrier, and `drain_for_shutdown()` is it: a process that exits without draining loses the
last thing each session did, which is exactly what a reader goes looking for after a
restart. Blocking is correct here where it is wrong for a producer -- the shutdown path has
nothing left to keep responsive -- and it is bounded (`_SHUTDOWN_DRAIN_SECONDS`), so a
wedged filesystem delays the exit rather than hanging it. If the writer pool is already gone
by then, the drain finishes the work in its own thread instead of losing it. Two
registrations cover the two ways a gateway ends: the dashboard's `on_cleanup` hook for a
graceful stop, which runs the drain off the loop, and an `atexit` handler for every other
exit -- a CLI run, a cron subprocess, a signal the server never sees.

The `atexit` handler is registered on the FIRST drain pass, not at import. This module is
reachable from the gateway boot path, and AUTOSDE's `no-new-work-on-gateway-boot-path` rule
asks for an optional subsystem's import to be gated rather than only its calls, so a launch
with the flag unset must register nothing and load nothing. Registering on first use also
keeps the ordering the drain depends on: `atexit` runs handlers last-registered-first, and
the registration happens immediately before the writer pool is first asked for work, so the
pool's own handler still runs ahead of this one.

There is no second stream to reconcile with. The parallel lifecycle-event package this
module was designed alongside is retired, so `kiro_crew.crew_log` is the only structured
record of a session's history and this module is its only writer;
`docs/request-for-change/rfc-append-only-ledger.md` is the decision of record.
