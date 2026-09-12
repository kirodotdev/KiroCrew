# Session Ledger Emitter

`session_ledger_emit.py` turns the gateway's ACP turn lifecycle into the append-only
per-session ledger `kiro_crew.ledger` keeps for each ACP session id. It is a **writer
only**: it owns which facts matter and where they are known, not storage or its layout.

The emitter is gated behind `KIROCREW_SESSION_LEDGER=1` (`constants.env_flag_enabled`,
read per call, truthy set `{1, true, yes, on}`) and defaults **OFF**, so this module is
inert until a later change turns it on. With the flag off no ledger directory is created
and no call reaches the storage library.

There is ONE flag. An earlier design gated streamed deltas behind a second one; that
emitter does not exist, for the reason "Bodies, and the flag" gives below, so there is
nothing left for a second flag to govern.

**Off is free, not merely silent.** Every entry point that hashes a payload or redacts a
body checks the flag ITSELF, before doing that work — `on_tool_called`,
`on_tool_completed`, `on_message_received`, `on_message_sent`, `on_message_steered`,
`on_request_configured`. Hashing is proportional to payload size and
redaction walks the whole body, both once per frame on the event loop, so leaving the check
to the writer would charge every user for a feature that is off by default.

## Identity

The ledger key is the **ACP session id**, not the slot key. A slot outlives its ACP
session: an agent switch, a model switch, or a project change calls
`SessionLifecycle.reset`, which pops the live session and lets the next turn cold-start a
fresh one. When that reset cleared the conversation the successor has a new id and a new
ledger; when it did not, the next cold start resumes the same id via `session/load`, so
`Ledger.exists` decides between create and open and a resumed session never truncates it.

`owner` and `agent` are header fields, written once at create time. There is no turn id in
the repo, so a turn is identified by its message boundary, `len(slot.messages)` at turn
start, and every entry of that turn carries it as `data.turn`. That ordinal, plus
`data.step` on a tool call, is the grouping identity: both are known at emit time, so an
entry answers what happened in its turn on its own. The envelope's `thread` field is left
unset here -- see "Reconnect is not resume" below for what it is for.

## Emitted facts

| Fact | Site | Data |
|---|---|---|
| `session/opened` | after `get_or_create`, on create or re-attach only | agent, slot key, model, cwd, `resumed` |
| `turn/started` | after every dispatch gate, immediately before the stream opens | turn ordinal, actor, prompt depth |
| `turn/refused` | each gate that refuses the dispatch | turn ordinal, actor, `reason`, prompt depth |
| `turn/completed` | the `EVENT_COMPLETE` arm, beside `_emit_turn_metric`; the turn's `finally` when no terminal event arrived | the four `TurnUsage` token counts, credits, `duration_ms`, `stop_reason`, model, provider — or `stop_reason: "failed"` with `error` and no usage |
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

`tool/called` and `tool/completed` gained payload accounting: `args_hash` and
`args_bytes` on the call, `result_hash`, `result_bytes` and `is_error` on the
completion. The digest stands IN PLACE OF the payload, never beside it -- arguments
and results are the likeliest fields to carry a credential or someone's data, and
this log is meant to be safe to surface. What a digest still answers is whether two
calls were made with the same arguments and whether a retry returned the same
bytes, which is what a cost or loop investigation actually asks. Keys are sorted
before hashing, so the same arguments hash the same however the dict was built.

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
drops the handle, the config echo baseline and the attempt counts -- what a successor
must not inherit -- and, of the open tool calls, only those whose turn is already
gone. Dropping all of them left a live turn's calls open for the life of the file,
because its own `close_open_tool_calls` then found nothing to close.

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

### `message/steered` has no emitter, and its position is why

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

Both facts have to be owned by one resolver before either site can write this entry,
which puts it in the same "specified, unwritten" group as `skill/*`,
`background/completed`, `subagent/*` and `approval/*`. Until then the type is specified
and its emitter API exists with no caller.

Nothing about the steer is lost. Its TEXT reaches the log as the `message/received` of
the turn that runs it, which is what the transcript shows too. And the requeue path
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

The map is SEEDED from the file when a claim re-attaches to an existing ledger,
because the in-memory map cannot survive the restart that makes a collision
possible: without seeding, a rerun after a restart writes attempt 1 at an ordinal
whose file already has an attempt 1. Seeding reads that session's `turn/started`
entries once, on the writer thread, on the resume path only, and a file it cannot
read leaves the map empty rather than failing the resume. `attempt` is omitted at 1,
which is every turn that was never rerun.

## Every tool call gets its closer

A tool that produces NO OUTPUT sends no result frame at all, so `tool_final` never
arrives and the completion path never runs. Its `tool/called` would then stay open
for the life of the file, and a fold counting open calls would report a turn that
never finished with a tool it had in fact finished with -- the same shape the
interrupted-turn repair exists to fix, which a live turn must not produce.

`close_open_tool_calls()` closes whatever is still open, and it is called at the two
points where the runner already makes this inference for the transcript: the
tool-group-to-text transition, where text after a group means every tool in it is
done, and the turn's terminal event. The closer carries `result_bytes: 0` and no
`result_hash`, because the tool genuinely produced no bytes -- a different claim from
"the payload was not recorded", which is an absent field.

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
log's tail is short by them -- and the warning names the buffered count and whether a
batch was in flight, so the gap is attributable rather than silent.

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

Written any earlier, the first step would land on refused turns too. `turn/started` waits
for the permit, shutdown and stop-before-dispatch gates for exactly that reason, and a
`step/started` in front of them would record a model call for a turn the model was never
invoked for — with no `step/completed` ever following, since the refusal path returns. It
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

A chunk group is written as ONE batch -- `Ledger.append_many`, one lock, one write,
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
badly — a refused chunk aborts the job, so no `message/sent` follows it either.

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
injected ledger context -- have no opening marker for `split_blocks` to classify, so
their characters land in its unclassified bucket, which this entry reports as a
single `other` source. Three zeroed sources would claim a measurement nobody took.

## Deferred to the next change, and why they are one problem

Five families the design specifies are not emitted here. Three fail for the same
reason `approval/*` already does -- **this log is keyed by the ACP session id, and
their sites hold only a slot key or a transcript key** -- and one fails because no
single site owns both halves of the fact it would state.

| Family | Why not yet |
|---|---|
| `tool/searched`, `tool/loaded` | Tool search runs inside the backend CLI. This process only writes the config overlay that enables it, so the query, the hit count and the loaded specs never enter the gateway at all. |
| `skill/searched`, `skill/loaded` | `skill_search` resolves a session KEY, and both context builders take `session_key`; the skills loader takes only `project_dir`. No ACP session id reaches any of them. |
| `background/completed` | Every background model call runs on the shared `_bg` session. The session the work is FOR is known by slot or transcript key, so there is nothing to key the entry by. |
| `message/steered` | Only the `steering_consumed` echo proves a turn consumed the text, but the text the steer interrupted is logged from the handler's segment cut, and the two race, so either site alone writes an entry whose seq contradicts causality. |
| `subagent/*` | Spawn holds `parent_session_key`, a slot key; the child's own ACP session id is assigned later, so a spawn-time pointer into the child's log cannot exist yet; and subagent runs have no token or credit accounting to record. |

So the next change is not more emitters. It is one slot-key-to-ACP-session-id
resolver, which unblocks four of the five at once -- including the approvals PR 1 deferred
for this exact reason -- where inventing it per family would build the same
plumbing three times.

Every compaction reaches the ledger on exactly one of those two paths. A reading kiro-cli
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
| `chat_handlers._api_chat` | dashboard composer | `user` |
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

Two shared helpers pass no actor on purpose: `spec_builder.runtime.enqueue_or_run_prompt`
takes both the message and its origin as parameters, and `_api_chat` serves an app backend
as well as a person. Their actor is their CALLER's fact, so hardcoding one here would
record a guess; they inherit `user` until the caller supplies it, which is the same
default every unaudited path has.

`approval/requested` and `approval/decided` are DEFERRED. The functions exist and are
tested, but no site calls them and none can yet: `ApprovalCoordinator` carries a slot key,
not a session id, so there is nothing to key an entry by. They land with that plumbing.

## What is deliberately not recorded

Tool arguments and tool results are recorded as a DIGEST and a byte count, never as
bytes; message bodies ARE recorded, redacted, and split when they exceed the line cap.
A tool call is identified by
its `tool_call_id` inside `data`, not by `ref`: a `Ref` cites lines of another ledger, and
an ACP frame is not a ledger unit. That id is the join key the transcript already uses.

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
once per process at warning level, and demoted to debug afterwards so a broken ledger
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
overtaking its own earlier entry. The backoff is per session, so one wedged ledger paces only
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

A REFUSAL is not retried at all. A `LedgerError` is the storage layer declining the entry
against the format -- an over-cap line, an unowned type -- decided before a byte is written, so
the file is byte-identical and the same entry is declined identically every time. Retaining
one would spend the whole attempt budget on a verdict that cannot change and hold that
session's whole log behind an entry that can never land, so it is dropped and counted at
once, and the pass continues past it: the entries behind a refused one are not waiting for
anything.

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
storage call to `executors.ledger_executor()` -- a pool of exactly ONE worker, because the
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
before the entries that cite it; that is the crew ledger's shape, not this one's.

## Reconnect is not resume

Open handles are cached with a bound, so a handle can go missing and the next emit
reopens the ledger. That reopen is a RECONNECT and it never asks for the interrupted-turn
repair: a handle absent from the cache says nothing about the writer's health, and the
turn is typically still running, so closing it would append a `turn/completed
{interrupted}` mid-turn and then keep writing past it.

The one caller that DOES repair is the resume: `on_session_opened(..., resumed=True)`,
which means this claim re-attached to a conversation a different gateway process was
writing. A turn left open in that file belongs to a writer that is gone, so closing it
records what happened. A warm reuse inside this process passes `resumed=False` and repairs
nothing.

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

A lifecycle record is never dropped for BACKPRESSURE. A hole in an append-only log is
permanent and silent: a reader cannot tell a turn that never completed from one whose
completion was discarded, which is precisely the distinction this ledger exists to record.
So there is no cap on the buffer past which an entry is thrown away, however far behind the
writer falls. An entry is given up on only when the filesystem refuses it or keeps refusing
to take it -- see "Retention" below -- and `dropped_writes()` is then the exact count.

The event loop is never BLOCKED. A producer appends its entry to an in-memory buffer and
returns -- it never writes, and never waits for the writer. So a filesystem that has stopped
keeping up costs memory rather than turn latency. An earlier design let a saturated producer
wait for queue room and, past a deadline, append for itself; both of those put a turn behind
the disk, which is the thing this path exists to prevent.

Together those force the buffer to be bounded by MEMORY rather than by policy. Crossing
`_PENDING_HIGH_WATER` is therefore reported and nothing else -- shedding is what the first
rule forbids -- and `buffered_writes()` and `peak_buffered_writes()` make the backlog
readable rather than a thing to be inferred from a log line. The buffer is keyed per session,
because each session is a separate file and one slow ledger must not reorder another's
entries.

**The one place memory is shed, and why it has to exist.** Every loss described above is
bounded by ATTEMPTS: an append raises, its batch is retained, and a fixed number of failed
passes gives up on it. A write that HANGS -- never returning, never raising -- reaches none of
that machinery, because the counter only moves when a call comes back. Producers meanwhile
keep appending, so nothing bounds the buffer at all. From the producer side a filesystem that
never answers is indistinguishable from a slow one, and the second must not be punished, so
the only available bound is a ceiling: `_MAX_PENDING_PER_SESSION` entries or
`_MAX_PENDING_BYTES_PER_SESSION` of serialized bytes, whichever is reached first.

Past it, the NEWEST entry for that session is dropped and counted in `dropped_writes()` --
the entries already buffered are older facts, and a log missing its most recent line is a
shorter record while one missing a line in the middle is a false one. It is reported once per
session, with the recovery its own single line when a write for that session lands again, the
same discipline the refusal path uses. This is the only shedding in the module; a busy disk
still costs memory and nothing else.

Because that state is otherwise invisible, the writer publishes what it is working on and how
long it has been at it. A job past `_WRITE_STALL_SECS` is reported once as a stall -- by a
PRODUCER, since the thread that would notice is the one blocked in the call. A stalled job is
not a failed one: it may still land, and its retry budget has not moved because nothing
raised. The line exists so an operator sees the condition the ceiling is otherwise absorbing
quietly.

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

`kiro_crew.events` is not a second emitter to reconcile with. Its only constructor
today is a read-only schema validator (`events/backfill.py`), which this module does not
route through and does not touch. The two envelopes are field-compatible -- this stream's
`type` is that one's `kind`, its `time` is that one's `ts_ms` -- so a projection folds both
with a field rename; `docs/request-for-change/rfc-append-only-ledger.md` is the decision
of record for which facts go to which stream.
