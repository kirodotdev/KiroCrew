# Ledger Core

Owners: `kiro_crew.ledger` (`schema.py`, `store.py`, `errors.py`)

## 1. Purpose

`kiro_crew.ledger` gives a crew or a session one durable, ordered, citable record of what happened. It is the storage layer only: it defines a file format, enforces who may write what into it, and reads it back. It carries no routes, no MCP tools, no dashboard surface and no migration.

The problem it answers is that long-horizon work keeps its state in a context window, which harness-owned compaction summarizes lossily. Transcripts are not a substitute: rotation, compaction and consolidation rewrite the whole file, the grain is a message rather than an operation, and no field defines an order a consumer can fold on. An append-only file with a writer-assigned sequence inverts that -- the record is the authority, the context is a cache -- and lets one unit cite a segment of another's history instead of copying it.

## 2. Relationship to `kiro_crew.events`

These are two layers of one story, not two competing logs, and the split is deliberate.

`kiro_crew.events` is a **global lifecycle stream**: one envelope (`v`, `kind`, `src`, `key`, `ts_ms`, `data`), day-sharded under `events/`, joined across domains by its `key`. Its own contract records that it carries no ordering field because ordering "needs a defined scope (per writer? per key? global?)" and would arrive "with the first emitter". `kiro_crew.ledger` is a **per-unit record**: one file per crew or session, where the scope question is already answered by the file itself, so `seq` is contiguous *within that file* and means something a global stream cannot give it.

That per-file scope is what the ledger adds, and it is why the two are not merged. `seq`, `thread` (a grouping key naming an earlier seq in the SAME file) and `ref` (a pointer into another file) are all defined relative to a single unit's ledger. Putting them in the global stream would require either a per-key sequencer inside a day-sharded multi-domain file, or a `seq` whose scope varies by `kind` -- the ambiguity that stream deliberately refused.

Which stream a future emitter writes:

| Emitter records | Stream | Why |
|---|---|---|
| One unit's own history, needing order, threading or citation | `kiro_crew.ledger` | The ordering scope is the unit's file. |
| A cross-domain lifecycle fact folded by correlation key | `kiro_crew.events` | No per-unit order is needed; the join axis is `key`. |

No emitter double-writes. The two envelopes are field-compatible on purpose -- the ledger's `type` is the stream's `kind`, its `time` is `ts_ms`, both `domain/action`, both epoch milliseconds -- so a projection that wants one timeline folds both with a field rename and no semantic translation.

Both of the events track's pending decisions are settled by this module, and `events/base.py` records that in place of the deferrals:

- **Which stream a future emitter writes.** This one, whenever it needs order, threading or citation. The global stream stays for an unsequenced cross-domain fact whose join axis is `key`.
- **The scope of ordering, and who assigns it.** `seq` is scoped PER UNIT -- one contiguous sequence inside one file -- and is assigned by the single writer of that file, under its lock. A day-sharded, multi-domain shard cannot answer that for itself: it would need either a per-key sequencer inside a shared file, or a scope that varies by `kind`. That is why the answer arrives with a per-unit file rather than as a convention on the global stream, and why `seq` stays out of that envelope instead of being added to it.

## 3. Storage and identity

```
<data home>/ledgers/crews/<store name>/ledger.jsonl
<data home>/ledgers/sessions/<store name>/ledger.jsonl
.lock                                                # sibling, per ledger
.lease                                               # sibling, per ledger
```

`<store name>` is `session_ledger._store_name()` -- a readable fold plus a digest of the exact id -- and the raw id lives in the header. The id is deliberately not the directory name: a channel session key legitimately carries a colon (`slack:1712793600.123`), which POSIX accepts and Windows refuses, so the raw id as a filename turns a sanctioned id into an `OSError` on a supported platform. Identity is the digest, so `Foo` and `foo` get distinct directories on a case-insensitive filesystem. `store.ledger_dir()` refuses a separator or NUL in the raw id and requires the resolved path to stay below the root, so a folded name cannot traverse. `Ledger.open()` proves it reached the right unit by checking the id the header stores.

Every segment repeats that header. Before `Ledger._iter_segments()` yields any entry from a segment, it requires the header's unit id, kind and schema version to match the opened ledger, and requires the filename's first sequence to match the physical first entry when that record is readable. A provenance failure refuses the segment and names it; an unreadable record retains the per-record skip behavior.

Every kind lives under ONE `ledgers` leaf, and that leaf is what carries the protection. It is an entry in `security.paths._CREW_SECRET_LEAVES`, so the agent's own file tools are refused, and an entry in `sandbox._CREW_HIDDEN_LEAVES`, so the OS hides it from every sandboxed spawn. Both are needed and neither substitutes for the other: the write-side rules below bind only callers who go through the library, the file-tool floor answers only the agent's tools, and a spawned subprocess that calls `open()` is answered by neither. Without all three an agent could forge an entry attributed to `src:"gateway"` or rewrite the history a conductor is designed to trust.

Session ledgers do **not** live under the flat `sessions/<key>.jsonl` transcript root, which they could otherwise share without shadowing. That root carries neither fence -- it is not in `_CREW_HIDDEN_LEAVES` (the `sessions` deny that exists is the private-member view's, which covers one narrow population) -- so a session ledger there was write-protected by the tool gate alone. Naming the shared root instead of one leaf per kind also means a third unit kind inherits both fences rather than needing a reviewer to notice it was left out.

The `ledgers` root is established EAGERLY, and that is what makes the mask non-vacuous. Both fences are stated per PATH, and the Linux bind-mask loop skips a leaf that does not exist -- so a root created lazily on the first write is unmasked in every sandbox spawned before it, one of which can then create the directory itself and fill it with entries a reader would take as the gateway's. Two mechanisms close that: `ensure_data_home()` creates it `0700` at startup, and `chmod`s it too, since `mkdir`'s mode is umask-masked on creation and a no-op on a directory that already exists; and `sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES` materialises it empty before every namespace spawn so the bind always has a name to cover. macOS needs neither: a Seatbelt deny is a path rule that holds for a name that does not exist yet.

## 4. Format

Line 1 is the header; every later line is an entry.

```json
{"type":"crew","version":1,"id":"qa","createdAt":1789000000000}
{"type":"crew:qa/report","seq":388,"time":1789000000000,"src":"crew:qa","thread":120,
 "ref":{"unit":"session","id":"s-7f3a","from":40,"to":96},"data":{"item":"pr-4127","status":"done"}}
```

| Field | Meaning |
|---|---|
| `type` | `domain/action`, or a guest-namespaced `crew:<name>/<action>` / `app:<name>/<action>`. |
| `seq` | Contiguous from 1 after the header. Writer-assigned. |
| `time` | Epoch milliseconds. Writer-assigned. |
| `src` | `gateway`, `acp`, `dashboard`, `patrol`, `session:<id>`, `crew:<name>` or `app:<name>`. |
| `thread` | Optional. The seq of an earlier entry in this same file -- a grouping key, like a chat thread id. |
| `ref` | Optional. `{unit, id, from, to?}`, a pointer to a segment of another (or the same) ledger. `to` absent means one line. |
| `ignorable` | Optional, `true` only. The writer's promise that a reader which does not know this `type` may skip the line. Absent on every entry that does not set it. |
| `data` | A JSON object. |

A crew header carries nothing else. A crew's display name and template belong to the members store, which owns them and can change them; an append-only line cannot, so duplicating them here would make this file the system of record for values it has no way to update, and the first rename would leave a permanent lie on line 1.

A session header additionally carries `owner`, `agent`, and the optional `task`, `pack`, `slot`, `thread` (`{crew, seq}`), `cwd` and `remote` -- the facts fixed for the session's whole life, which a reader needs before reading any entry.

## 5. The frozen session-log format

**Types and fields are additive-only from this commit: a type may gain an emitter later, never a
different shape.** The format is frozen here, in the commit that introduces it, so that every later
change is an emitter landing against a schema that already accepts it. That is why the tables below
list types nothing writes yet -- writing one must never require reopening the format.

Every type is `domain/<past participle>`, a fact that happened. Every turn-scoped entry carries
`data.turn`, and `data.step` where a step exists. `thread` stays unset on session entries.

The **Emitter** column says what exists in this commit. `yes` means the gateway writes it today;
`—` means the type is owned, writable and specified, and nothing writes it yet.

### Session, turn
| Type | `data` | Emitter |
|---|---|---|
| `session/opened` | header echo + `resumed` | yes |
| `session/seeded` | `{source, count}` — history imported from a legacy transcript | — |
| `session/closed` | `{reason}` | yes |
| `turn/started` | `{turn, actor, depth, message_seq?, attempt?}` | yes |
| `turn/refused` | `{turn, actor, reason, depth}` | yes |
| `turn/completed` | `{turn, depth, stop_reason, duration_ms, credits, model, provider, tokens{input,output,cache_read,cache_write}}`; `error?` and no `credits`/`tokens` on a turn that ended without its terminal event | yes |
| `write/dropped` | `{dropped_count, dropped_bytes}` — one durable account of writer losses before later entries resume | yes |

`write/dropped` records a hole without creating one in `seq`: the run remains contiguous across the
loss because no line was appended for a rejected or exhausted job. The marker is the evidence that
facts are missing. The next drain for that session puts it ahead of every ordinary entry; more loss
before it lands is merged into the same marker, and a marker that is itself dropped carries its
counts into the next one. It carries no reason code: every loss marks, and the one site that knows a
cause cannot separate them — `_permanent` collapses a malformed entry and an entry refused because
another process owns the log into a single boolean. What a reader can act on is that facts are
missing and how many.

**Turn identity under regenerate and rewind.** A turn is identified by `data.turn`, the message
boundary at its start, which is stable and needs nothing looked up -- but a regenerate or a rewind
runs a turn at an ordinal the log has already used. `attempt` is the discriminator: an int
defaulting to 1, incremented per rerun of the same ordinal, and omitted from the entry at 1 because
that is every turn that was never rerun. Without it two starts at one ordinal are
indistinguishable, and a fold cannot tell a deliberate rerun from a duplicate write -- which want
opposite handling. The pair `(turn, attempt)` is therefore the identity a fold groups on, and
`(turn, attempt, step)` locates a single model call.

### Message, request, step

| Type | `data` | Emitter |
|---|---|---|
| `message/received` | `{turn, role, text, source, attachments:[ref]}` | yes |
| `message/sent` | `{turn, step, text, usage, interrupted?, chunks:[seq]}` | yes |
| `message/chunk` | `{turn, step, delta}` — an oversize body's slice | overflow only |
| `message/queued` | `{source, bytes, queued_seq}` — arrived while a turn ran | yes |
| `message/steered` | `{turn, mode: interrupt \| follow_up, text}` | — |
| `request/configured` | `{turn, model, provider, context_window, system?, system_bytes?}` — written on change only | yes |
| `context/composed` | `{turn, step, sources:[{kind, chars, tokens}], chars, tokens, tokens_estimated}` | yes |
| `step/started` | `{turn, step}` — one model call | yes |
| `step/completed` | `{turn, step, ms}` | yes |

`context/composed` is where the per-turn bill for what the gateway injects lands: `sources` names
every block put in front of the model — `system`, `memory`, `lessons`, `skills_index`, `steering`,
`project`, `tool_specs`, `ledger_context` — each with its `tokens`.

### Tool, skill, approval

| Type | `data` | Emitter |
|---|---|---|
| `tool/called` | `{turn, step, call_id, name, server, kind, args_hash?, args_bytes?}` | yes |
| `tool/completed` | `{turn, step, call_id, status, is_error?, elapsed_ms, result_hash?, result_bytes?}` | yes |
| `tool/searched` | `{turn, query, hits}` — lazy MCP discovery | — |
| `tool/loaded` | `{turn, server, names:[..], spec_tokens}` | — |
| `skill/searched` | `{turn, query, hits}` | — |
| `skill/loaded` | `{turn, name, path, tokens, via: index \| search \| pointer}` | — |
| `approval/requested` | `{turn, id, tool, reason}` | — |
| `approval/decided` | `{turn, id, decision, by}` | — |

Arguments and results are DIGESTED, never recorded: `args_hash` / `result_hash` are the sha256 of
the serialized payload and `args_bytes` / `result_bytes` its length. That answers "same arguments as
last time" and "how large was this" without the ledger becoming where a shell command's secrets and
a file's contents accumulate. Bodies, if they ever land, arrive as their own change. Each pair is
absent rather than zeroed when there is nothing to digest, since 0 is a real size. `is_error` is
tri-state and absent when the caller did not say, because "nobody asserted this worked" is not the
same claim as "it worked".

Approvals have no emitter yet for a reason rather than a schedule: the approval coordinator carries
a slot key, not a session id, so there is nothing to key an entry by.

### Model, compaction, plan, placement

| Type | `data` | Emitter |
|---|---|---|
| `model/selected` | `{turn?, model, source}` | yes |
| `compaction/applied` | `{turn, pct_before, pct_after, freed_pct}` | yes |
| `summary/written` | `{turn, text, covers:{start_seq, end_seq}}` — the fold knows what it replaced | — |
| `plan/updated` | `{turn, items:[{id, text, state}]}` — the session's own task list | — |
| `remote/placed` | `{provider, id}` | — |
| `remote/lost` | `{reason}` | — |

### Background and children

| Type | `data` | Emitter |
|---|---|---|
| `background/completed` | `{kind: title \| memory_consolidation \| summary \| digest, model, tokens, credits, ms, result_ref}` | — |
| `subagent/spawned` | `{turn, agent_id, agent, model, scope:{memory, lessons, project}}` + `ref` into the child's log | — |
| `subagent/steered` | `{agent_id, mode}` | — |
| `subagent/completed` | `{agent_id, tokens, credits, ms}` | — |
| `subagent/failed` | `{agent_id, reason}` | — |

These are the families a single-agent runtime never needs and a gateway does: every token spent on a
session's behalf, whether a person asked for it or not, is a fact in that session's log attributed to
what caused it. A subagent is itself a session with its own ledger, whose header `thread` points at
the parent's `subagent/spawned` entry while that entry carries a `ref` into the child's log — the
same pair as a crew dispatch, one level down.

## 6. Rules

Every refusal is a `LedgerError` carrying a stable `code`; the codes are API surface and are additive-only.

**Ownership** answers whether a kind of unit has such events at all. `schema.TYPE_OWNERSHIP` maps kind to owned `type` domains -- crew: `member` `activity` `slot` `patrol` `message` `crew` `item` `memory`; session: `session` `turn` `step` `tool` `approval` `model` `compaction` `summary` `plan` `remote` `message` `request` `context` `skill` `background` `subagent` -- and anything else is `event_type_not_owned`. It is prefix-based, so a new action under an owned domain needs no change. `message` appears in both registries, which is what ownership means: a crew forwards messages and a session records its own bodies, so both kinds have such events and neither name is a collision.

**Namespacing** answers whether an emitter may write it. A `crew:<name>` or `app:<name>` emitter is a guest: it may write only under its own prefix, and only into a crew ledger, else `namespace_violation`. A guest type is judged by this rule *instead of* ownership, which is why the registry needs no guest entries -- a guest's own name is its permission.

The remaining bounds: `thread` must name an existing, parseable, earlier seq (`bad_thread`); `ref` must be well-formed and span at most `MAX_REF_SPAN` lines (`bad_ref`); a serialized entry must be at most `MAX_ENTRY_BYTES` (`entry_too_large`). Caps refuse rather than truncate, leaving the file byte-identical -- a clipped record the caller believes landed intact is a loss the caller cannot detect.

**An unknown type is the reader's rule, and the writer declares the exception.** `iter_from(known=...)` is a reader stating the types it can interpret; without that argument nothing changes and every entry is yielded, which is what every caller predating the marker gets. With it, an entry whose type is not in the set is skipped when the writer marked it `ignorable: true`, and raises `unknown_entry_type` when it did not. The asymmetry is the point: skipping an unknown KEY loses a detail, while skipping an unknown LINE can lose the plot, because a required entry a reader cannot interpret may change the meaning of every entry after it. A fold that continued past one would return a confident wrong answer instead of an admitted failure, so the refusal names the seq it stopped at and the reader can resume there once it learns the type.

Only the writer can make that promise, since only it knows whether the entry samples a stream or states a fact -- so the marker rides on `append`, never on the read. Read-back is strict: a literal `true` sets it and anything else reads as absent. The marker RELAXES a guard, and this tree is agent-writable, so a truthy coercion would let a damaged or planted line switch the guard off with a string or a number.

The gate is on `iter_from` alone. `page` and `resolve` render history for a person or drill into a citation, where displaying an unfamiliar line is a missing detail rather than a corrupted fold, so neither takes a vocabulary and neither refuses.

## 7. Append-only guarantee and damage

A line is never rewritten. Exactly one mutation exists: on `open`, trailing bytes that are not a complete line are dropped. Termination decides which those are, so the rule needs no heuristic. Every append writes `line + "\n"` and fsyncs, so unterminated bytes that fail to parse are a crash artifact and go; unterminated bytes that *do* parse lost only their newline, so the record stays and the next append re-supplies the separator. A terminated line that does not parse is damage inside history: reads skip it, the file keeps it. Two readers of the same bytes therefore always agree.

A failing write ROLLS ITSELF BACK. Bytes reach the file before the fsync runs, so a failure anywhere in the write-then-sync sequence leaves an outcome nobody knows: the entry may well be durable. That matters because the write-behind retains and retries a failure, and a retry against an unknown outcome appends the same fact a second time under a new seq -- a duplicate no reader can tell from a real repeat, in a file nothing rewrites. So the append truncates the file back to the size it had before the attempt and re-raises, which makes the failure definite: either the append is whole and synced, or it is gone and the retry writes it cleanly. A partial write is removed for the same reason, and one more -- leaving half a line behind would defer the cleanup to the next append's torn-tail check, so the file would carry a fragment until then.

That truncation is not a second exception to the never-rewrite rule. The bytes removed are the caller's own failed append, so the file is restored to a state a reader could already have seen rather than edited; it is the torn-tail rule applied at the moment the tear happens instead of at the next open. It is safe against a concurrent writer because the append lock is held across the write and its rollback, so no other handle's entries can lie inside the range.

If the rollback ALSO fails -- likely, since whatever broke the write is often still broken -- the file may hold bytes no entry claims, and `IndeterminateAppend` reports exactly that. The residue is an unterminated or unparseable tail, which is the shape the next `open` truncates, so the recovery already exists; the distinct type is so a caller can tell "nothing happened" from "something may have".

The header is the exception, and only at creation: `Ledger.create()` publishes it with `atomic_write` (temp file, fsync, rename), so the file is either complete or absent and a crash or ENOSPC mid-header cannot leave a fragment. Without that, a fragment would be read as a torn tail, truncated to an empty file, and then refused by `open` while `create` refused the very file it had produced -- a unit wedged with no automated recovery. For the same reason all three of `create`, `open` and `exists` read a **zero-byte file as absent**: it carries no header and no entries, so there is nothing to protect and one shared meaning is what keeps the paths from disagreeing. After the rename the file is append-only for the rest of its life.

Decoding is **strict and per line**, on bytes read in binary mode. Replacement-decoding would be the wrong tolerance: invalid bytes inside a JSON string can decode into still-valid JSON, so a damaged line would be yielded with `U+FFFD` substituted into its values rather than skipped -- handing a consumer altered data as authority, which a record that calls itself the authority must never do. Byte damage is this machinery's expected input, so an undecodable line is damage and is skipped exactly like unparseable JSON; an undecodable header is `bad_header`.

Both reads are FRAMED by `jsonl_util`, so one planted line cannot cost more memory than the format's own write limit (`MAX_ENTRY_BYTES`) however long it is -- the tree is agent-writable, so an unbounded read is a memory bound an attacker chooses. The two use different postures on purpose. Entries take the SKIP posture (`bounded_raw_records`), matching the rule above: an over-cap line is not something this writer produced, so it is damage and costs that one line. The header takes the ABORT posture (`strict_raw_records`), because skipping an over-cap line 1 would hand back line 2 -- an entry -- as though it were the header. That is a wrong answer rather than a missing one, so an unreadable line 1 becomes `bad_header` and the unit's identity fails closed.

### Interrupted turns

A header naming a format version this build does not read is refused BEFORE the rest of the header is checked and before any row is decoded, with its own code (`unsupported_version`) and a message telling the reader to upgrade and stating the file is not damaged. The ordering is the point: a newer format need not satisfy any of this build's structural checks, so its new required type would surface as `unknown_entry_type` and its header shape as `bad_header` -- both of which accuse the file of corruption when the truth is that the reader is old, and someone acting on that diagnosis deletes a healthy log. Refusing NEWER only; an older version opens as it stands, which is what a future migration is for. Nothing else in this module reads `version`, so without this check carrying it buys nothing.

A session ledger's newest turn can be left open by a crash, a `SIGKILL` or a pod eviction. `Ledger.open(kind, id, repair=True)` -- or `ledger.repair_interrupted_turn()` on a handle already held -- closes it, appending deterministic closers: a `tool/completed` with `status: "unknown"` for each unmatched `tool/called` inside that turn, in first-seen order, then `turn/completed` with `stop_reason: "interrupted"`. Calls first, because a turn cannot close while a call inside it is open, and closing them the other way would produce a record no live writer could have produced.

A group whose members are meaningless apart is written by `append_many`, which takes the file lock once, allocates a contiguous seq run from one read of the tail, and writes every line with one `write()` and one `fsync`. The one caller today is a body too large for a single line: the `message/chunk` entries plus the entry that CITES their seqs. The citation is built INSIDE the lock, by a `cite(seqs)` callable the caller passes alongside the chunks and which receives the run the call has just allocated, so a citation cannot disagree with the allocation it names. Allocating outside the lock and citing the result cannot be made correct, because the lock is a cross-process one: between that read and the write another handle can append and shift the run, after which `chunks` name the intruder's entries -- seqs that exist and parse, so no later read can tell the body apart from the right one. Appending the members separately leaves a different window, in which a hard kill puts the body on disk with nothing pointing at it -- stored and unreachable, with no record of the message at all. Every refusal still happens before any byte is written, including validation of what `cite` returns, so a rejected group leaves the file identical, and `thread` is not accepted because a group is self-contained and an anchor check would have to re-read the tail it is being allocated from.

One write can still tear, so the repair handles what a torn group leaves. A TRAILING run of `message/chunk` entries with no citing entry after it is unreachable, and `repair=True` truncates it -- the same allowed mutation as a torn tail -- with one warning naming the seq range. The residual is the one message that was mid-write, which is the residual of any single entry lost to a hard kill, and the freed seqs are reused by the repair's own closers so a fold sees a contiguous run rather than a gap. A chunk group followed by any other entry landed whole and is left alone.

That scan reads with the ABORT posture, because the offset it produces feeds a truncation. A bounded read drops an over-cap record in full, terminator included, without yielding it, so a byte walk over what such a read yields stops matching the file at the first oversized record and every offset after it is short by that record's length -- and truncating at a short offset cuts valid entries. So a record that cannot be delivered intact aborts the scan and nothing is truncated, with a warning naming why. The cost of failing closed is one unreachable body left in the file, against losing history that was never in question.

**Repair is opt-in, and only a RESUME may ask for it.** A plain `open` never mutates the record. The two callers of `open` want opposite things: a resume -- the gateway finding a session ledger whose writer is gone -- wants the open turn closed, while a live writer RECONNECTING to its own ledger must not have it closed, because its turn is still running and a `turn/completed {interrupted}` landing mid-turn would claim an outcome the turn never had and then be followed by more of that turn's entries. Nothing in the file distinguishes an open turn from a dead one, so only the caller's situation can: repair cannot be inferred from the fact that an `open` is happening. A reconnect happens for reasons unrelated to the writer's health -- a handle dropped from a bounded cache is enough -- which is precisely how an unconditional repair corrupts a live turn. The caller's situation is a belief about a writer it cannot see, which is why it is not the only check: see "Write ownership" below.

Every closer reuses the LAST REAL entry's `time`. A closer describes what happened when the writer stopped, not when some later process opened the file, so the current clock would put a gap of arbitrary length inside a turn and make any duration computed off these entries a measure of downtime. Reusing the time also makes the repair deterministic: the same bytes in produce the same bytes out, whenever it runs, and a second repair adds nothing because the tail is balanced.

This is still append-only -- nothing is rewritten and `seq` continues -- so a reader that already folded the file sees only new lines. It is scoped to the SESSION kind, whose turn lifecycle these types belong to, and to the OPEN turn: an unmatched call inside a turn that did complete is a different anomaly, and inventing a result for it would be the reader editing history it was not asked about. Best-effort, like the torn-tail repair: a closer that cannot be written leaves the tail open, which is the state every reader already tolerates.

**A content-aware repair reads with the abort posture, because its result decides whether history is added.** The ordinary reader skips a malformed interior line so one damaged line cannot hide the history in front of it, and that is the wrong tolerance here: a turn whose real `turn/completed` is byte-damaged reads as still open once the line is skipped, and a closer appended from that fold states an outcome no writer observed -- permanently, since later folds skip the damaged real completion too and see only the fabrication. So the fold reports every record it could not read, and a skip refuses the repair outright: the file is left exactly as it is with one warning naming the record. This is the same rule the truncation-offset scan already follows, and both share one classification of what counts as a record, because their results feed the same mutation -- one chooses where the file is cut, the other whether lines are added to it.

`seq` is read back from a bounded window at the file's end inside the per-ledger lock rather than trusted from an in-process cache, so two writers cannot both claim one number and the read costs the same on a ten-line ledger or a million-line one. `store._anchor_exists()` reuses that same window, so proving a `thread` anchor is parseable is free for a recent anchor and falls back to a scan only for one older than the window.

`resolve` has four outcomes and takes NO access callback. `ok` is a complete answer; `gone` means the cited unit has no ledger at all; `pruned` means the span reaches BELOW the oldest surviving segment's first seq, so retention removed it; `corrupt` means the span lies inside a segment that still exists yet read short, which is damage.

**Retention and damage are never reported as each other.** Both look identical from the caller's side -- a short answer -- so the classification is made from the segment names rather than the result: a span starting below `segment_first_seqs()[0]` is `pruned` when the part of it at or above that seq reads back whole, and anything else that reads short is `corrupt`. The surviving part is checked separately because a partly pruned span is short for a legitimate reason -- so the count that would catch damage in it is already satisfied -- and a citation whose head retention removed can still hold a damaged line further up, in a segment that is still on disk. Reporting that as retention hides the damage behind a normal answer. Getting this backwards is worse than either error alone. A reader told `pruned` stops looking, because retention removing old lines is a normal answer; told `corrupt`, it knows the file it still has is not intact. Reporting damage as retention therefore converts a recoverable alarm into silence. Citing PAST the newest entry is `corrupt` too, and deliberately not `ok`: a `Ref` always names a definite span -- absent `to_seq` means the single line at `from_seq`, so there is no open-ended spelling -- which makes a span reaching beyond the newest entry a claim about lines that are not in the file. `ok` means every cited seq was read back, so a short answer is the citation failing whatever shortened the file. That also covers the case with no torn bytes to give it away: whole lines cleanly truncated lower both the walked entries and a reopened handle's tail together, and an expected count taken from the file rather than from the citation would shrink to match what survived and answer `ok` with the cited lines gone.

This layer claims no authorization, so it has none to deny: a check defaulting to allow would make the shortest call shape the insecure one, and a check with no permission model behind it only looks like a boundary. Every caller today is in-process gateway code that can already read the file. A `forbidden` status arrives with the first caller that HAS a permission model -- the routes that mount this -- where the caller identity it must be derived from actually exists.

### Write ownership

A resume's belief that the previous writer is gone is not verifiable from the file, so it is not the only check. **Writes to a unit are owned, and the arbiter is the kernel.** The owner holds a non-blocking advisory lock on a `.lease` file beside the log, and a process that cannot take it is REFUSED with `already_owned` rather than made to wait: it appends nothing and repairs nothing, so the file keeps one writer's account of a turn instead of two interleaved ones. This is what stands between a resume in a second gateway and a turn that reads as completed-as-interrupted and then, further down, completed for real, with one tool call closed both `unknown` and `completed` -- a shape a fold cannot resolve and no later pass can undo.

Ownership is taken LAZILY, on a handle's first write, and never by `open` itself, because `open` also serves readers: `iter_from`, `page` and `resolve` need no ownership, and making a reader contend with the writer would buy nothing. `open(repair=True)` claims it before the closers, and that is the same rule rather than an exception -- the closers are appends. `create` claims nothing: it publishes a header for a unit that has none, and two processes racing it are already settled by `already_exists` under the per-append lock.

The lock is REFCOUNTED PER PROCESS, keyed by the lease file's path. That is a correctness requirement rather than an optimization: a POSIX lock belongs to an open file description rather than to a process, so a second `open()` of the lease path inside one process contends exactly as another process would -- and one process legitimately holds several handles for one unit, since the emitter's cached handle and the handle a session claim opens overlap while the cache entry is replaced. So the first writer in a process takes the kernel lock, every later handle shares it, and the last handle to be dropped gives it up. The path is the key rather than `(kind, id)` because the data home is repointable and the kernel locks a file, not a name. Acquire and release both run under one module lock, for the same reason the count exists: two threads reaching for one unit must share a descriptor rather than race two of them and have one refuse the other.

Release is bound to the HANDLE being dropped rather than to an explicit call, and that timing closes the window from both sides. The emitter's eviction rule never drops a handle belonging to a live turn, so ownership can only end BETWEEN turns -- and between turns there is no live turn for a successor's repair to damage. A terminal event is queued rather than written, so "between turns" begins when that entry LANDS, not when it is handed over: the emitter marks the turn's record as owing a closer at handover, and a re-claim leaves such a record alone while still closing one whose terminal was never emitted, since nothing else will ever close that one. Meanwhile a queued write that still holds the handle keeps the ownership it is about to need, which an eager release at eviction would have taken out from under it.

There is deliberately NO expiry. The case ownership is for needs none: a crashed, killed or evicted owner has its lock dropped by the kernel when its descriptor closes, so a successor takes ownership at once and closes the turn its predecessor left open. What an expiry would add is the power to expropriate a writer that is merely SLOW, whose next append then lands after a successor has already closed its turn -- the exact damage this prevents, reintroduced as a timeout. A live but wedged owner therefore keeps ownership until its process exits, and the claim it blocks reports its own loss instead of taking the log.

After locking, the held inode is compared with the file now at the lease path, and a mismatch starts over. A POSIX lock names an inode, so a lock on a path that was unlinked and recreated proves nothing about the file a writer is about to append to. Nothing in this tree removes a live unit's lease file; the check is what makes that a property of the code rather than an assumption about it.

## 8. Retention: segment files

A ledger is one or more SEGMENT files. `ledger.jsonl` is the segment beginning at seq 1; a later
segment is `ledger.<first_seq>.jsonl`, with its first seq in the name so ordering needs no file read.
A reader walks segments in ascending first-seq order and requires seq to stay contiguous ACROSS each
boundary (`segment_gap`), because two files are independent objects: a half-finished copy or a
deleted middle segment is invisible unless it is checked. Inside one file a missing seq is a damaged
line, which the read skips as it always has -- one unreadable record must not make the rest of the
file unreadable -- so the check deliberately does not apply there.

**Retention is deleting whole segments off the front, and it is not a format change.** That is the
reason for segments rather than one growing file: pruning old lines out of a single file would
rewrite it, and the guarantee this store makes is that a written line is never rewritten. Deleting a
segment leaves every remaining line byte-identical, so a pruned log costs a reader the old entries
and costs the format nothing. A first segment starting above 1 is therefore read as it stands rather
than refused -- a gap at the FRONT is retention, a gap in the MIDDLE is damage. The header travels on
every segment, so the oldest survivor still carries the facts a reader needs before reading any
entry, and `open` resolves it from there.

The reader side is implemented here: discovery, ordering, the continuity check, and opening a log
whose oldest segment is gone. `iter_from` and `resolve` span segments, because those are the paths a
fold and a citation take, and a fold that silently stopped at a segment boundary would be wrong
rather than incomplete. `get` and `page` span them too: reading the newest alone made a point read of a rotated
entry answer `None`, which a caller cannot tell from "no such entry", and made paging stop at the
boundary while reporting no cursor -- claiming the whole history had been seen when one segment had.
**No writer rotates yet** -- a writer appends to the newest segment, which is `ledger.jsonl` in every
ledger today. What creates the second segment, and on what trigger, is a later change that needs no
format change to land.

## 9. Scope

The first consumer is the session-ledger emitter (`docs/system-specs/modules/session-ledger-emitter.md`), which writes the ACP turn lifecycle behind the `KIROCREW_SESSION_LEDGER` flag. No crew writer exists yet, so the crew half of the ownership registry has no emitter; the guest namespace is why that is safe to leave open, since a guest crew or app needs no entry in it.

Read and write paths ship together deliberately: the guarantees this format makes -- contiguous seq under a lock, torn-tail repair, refusal before any byte is written -- are each a claim about what a reader sees after a writer acted, so neither half demonstrates them alone. `test/test_ledger_core.py` exercises them against real files rather than against a mock.
