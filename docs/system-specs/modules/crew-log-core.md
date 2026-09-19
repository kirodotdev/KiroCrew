# Crew log Core

Owners: `kiro_crew.crew_log` (`schema.py`, `store.py`, `errors.py`)

## 1. Purpose

`kiro_crew.crew_log` gives a crew or a session one durable, ordered, citable record of what happened. It is the storage layer only: it defines a file format, enforces who may write what into it, and reads it back. It carries no routes, no MCP tools, no dashboard surface and no migration.

The problem it answers is that long-horizon work keeps its state in a context window, which harness-owned compaction summarizes lossily. Transcripts are not a substitute: rotation, compaction and consolidation rewrite the whole file, the grain is a message rather than an operation, and no field defines an order a consumer can fold on. An append-only file with a writer-assigned sequence inverts that -- the record is the authority, the context is a cache -- and lets one unit cite a segment of another's history instead of copying it.

## 2. One stream, not two

There is one structured record of what happened, and it is this one. A parallel global lifecycle stream (`kiro_crew.events`, day-sharded under `events/`, joined by a correlation `key`) was retired unwritten: it had no emitter and no reader, no `events/` directory existed on any host, and each of its kinds names a fact this format already owns as a `type`. Two vocabularies for one fact is a choice every future emitter would have had to make, and the answer would have been "this one" every time.

`seq`, `thread` (a grouping key naming an earlier seq in the SAME file) and `ref` (a pointer into another file) are why. All three are defined relative to a single unit's crew log, so the scope question a global stream cannot answer for itself -- per writer? per key? global? -- is answered by the file. A day-sharded, multi-domain shard would need either a per-key sequencer inside a shared file or a `seq` whose meaning varies by kind.

A fact with no unit to belong to still has a home: a script cron, or gateway lifecycle, gets a `gateway`-kind crew log when something needs to record one. That kind does not exist yet, and adding it is a change to `KINDS` and `TYPE_OWNERSHIP` rather than to the wire format.

## 3. Storage and identity

```
<data home>/crew-log/crews/<store name>/log.jsonl
<data home>/crew-log/sessions/<store name>/log.jsonl
.lock                                                # sibling, per crew log
.lease                                               # sibling, per crew log
```

`<store name>` is `session_ledger._store_name()` -- a readable fold plus a digest of the exact id -- and the raw id lives in the header. The id is deliberately not the directory name: a channel session key legitimately carries a colon (`slack:1712793600.123`), which POSIX accepts and Windows refuses, so the raw id as a filename turns a sanctioned id into an `OSError` on a supported platform. Identity is the digest, so `Foo` and `foo` get distinct directories on a case-insensitive filesystem. `store.crew_log_dir()` refuses a separator or NUL in the raw id and requires the resolved path to stay below the root, so a folded name cannot traverse. `CrewLog.open()` proves it reached the right unit by checking the id the header stores.

Every segment repeats that header. Before `CrewLog._iter_segments()` yields any entry from a segment, it requires the header's unit id, kind and schema version to match the opened crew log, and requires the filename's first sequence to match the physical first entry when that record is readable. A provenance failure refuses the segment and names it; an unreadable record retains the per-record skip behavior.

Every kind lives under ONE `crew-log` leaf, and that leaf is what carries the protection. It is an entry in `security.paths._CREW_SECRET_LEAVES`, so the agent's own file tools are refused, and an entry in `sandbox._CREW_HIDDEN_LEAVES`, so the OS hides it from every sandboxed spawn. Both are needed and neither substitutes for the other: the write-side rules below bind only callers who go through the library, the file-tool floor answers only the agent's tools, and a spawned subprocess that calls `open()` is answered by neither. Without all three an agent could forge an entry attributed to `src:"gateway"` or rewrite the history a conductor is designed to trust.

Session crew logs do **not** live under the flat `sessions/<key>.jsonl` transcript root, which they could otherwise share without shadowing. That root carries neither fence -- it is not in `_CREW_HIDDEN_LEAVES` (the `sessions` deny that exists is the private-member view's, which covers one narrow population) -- so a session's log there was write-protected by the tool gate alone. Naming the shared root instead of one leaf per kind also means a third unit kind inherits both fences rather than needing a reviewer to notice it was left out.

The `crew-log` root is established EAGERLY, and that is what makes the mask non-vacuous. Both fences are stated per PATH, and the Linux bind-mask loop skips a leaf that does not exist -- so a root created lazily on the first write is unmasked in every sandbox spawned before it, one of which can then create the directory itself and fill it with entries a reader would take as the gateway's. Two mechanisms close that: `ensure_data_home()` creates it `0700` at startup, and `chmod`s it too, since `mkdir`'s mode is umask-masked on creation and a no-op on a directory that already exists; and `sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES` materialises it empty before every namespace spawn so the bind always has a name to cover. macOS needs neither: a Seatbelt deny is a path rule that holds for a name that does not exist yet.

## 4. Envelope

Line 1 is the header; every later line is an entry. This section is the part that is the same for both kinds -- the fields, the bounds, and what `ref` and `thread` mean. What belongs to one kind alone is in 4a and 4b.

```json
{"type":"crew","version":1,"id":"qa","createdAt":1789000000000}
{"type":"crew/report","seq":388,"time":1789000000000,"src":"crew:qa","thread":120,
 "ref":{"unit":"session","id":"s-7f3a","from":40,"to":96},"data":{"item":"pr-4127","status":"done"}}
```

| Field | Meaning |
|---|---|
| `type` | `domain/action`, or the one guest form `app:<name>/<action>`. A type carries the FACT; who wrote it is `src`. |
| `seq` | Contiguous from 1 after the header. Writer-assigned. |
| `time` | Epoch milliseconds. Writer-assigned. |
| `src` | The emitter. Which names a kind accepts is per kind: 4a and 4b. |
| `thread` | Optional. The seq of an earlier entry in this same file -- a grouping key, like a chat thread id. |
| `ref` | Optional. `{unit, id, from, to?}`, a pointer to a segment of another (or the same) crew log. `to` absent means one line. |
| `ignorable` | Optional, `true` only. The writer's promise that a reader which does not know this `type` may skip the line. Absent on every entry that does not set it. |
| `data` | A JSON object. |

A serialized entry is capped at `MAX_ENTRY_BYTES` (64 KiB) and a `ref` at `MAX_REF_SPAN` (500) lines. Both refuse rather than truncate; section 6 states why.

`thread` and `ref` answer different questions and the difference is the file boundary. `thread` groups entries INSIDE one crew log, so it is an int -- a seq this same writer assigned, which is why the anchor can be proved to exist before the append lands. `ref` points ACROSS files, so it must name a unit as well as a seq range, and it is resolved at read time with four outcomes rather than dereferenced at write time: the cited file can be pruned or damaged between the citation and the read, and the citing entry is not rewritten when it is.

`ref` is deliberately kind-independent: it is the envelope's, so a crew's log may cite a session's segment and a session's log may cite a crew's. Section 4b names the one bridge a writer takes today.

Two overlaps between the kinds are intentional and are not collisions. The `message` domain exists in BOTH kinds with different `data` -- a crew forwards messages, a session records its own bodies -- because ownership answers "does this kind have such events", and both do. And `ref` crossing kinds is the mechanism the two records are joined by, rather than one kind copying the other's bytes.

### 4a. The session's log

One session's own history: the ACP turn lifecycle, what was put in front of the model, what the model did, and durable work-state updates.

`src` is `gateway` or `acp`, and nothing else. Those are the two writers a session's entries come from -- the gateway around each turn, the ACP runtime for the measured path -- and a list wider than that is an authorization hole rather than a convenience, because `src` is what a reader attributes an entry to. A `patrol` or a guest crew has nothing to say inside one session's turn history. Adding a name is ADDITIVE: no reader validates `src`, so the list names the emitters that exist. `session:<id>` is spelled by no kind, because no emitter writes it.

`thread` stays unset. A session entry's grouping key is its turn, which it carries in `data.turn` (and `data.step` where a step exists) and which is known at emit time, so threading would be a second spelling of a fact the entry already states.

The type tables are section 5, which lists every session type, its `data` and whether an emitter writes it today.

A session header carries `owner`, `agent`, and the optional `task`, `pack`, `slot`, `thread` (`{crew, seq}`), `cwd` and `remote` -- the facts fixed for the session's whole life, which a reader needs before reading any entry. `owner` never changes. Among them, `thread {crew, seq}` is where a session's place in a crew's work lives: it points back at the crew entry that caused the session to exist, on line 1 rather than in an entry, because it is settled before the first turn.

### 4b. The crew's log

A crew's activity record: its members, its work items, the messages it forwarded, and the sentences a conductor reads. A crew entry is often a sentence plus a `ref` -- "I opened PR-4127" and where to read the work.

A crew header carries the unit's id and its creation time and nothing else. A crew's display name and template belong to the members store, which owns them and can change them; an append-only line cannot, so duplicating them here would make this file the system of record for values it has no way to update, and the first rename would leave a permanent lie on line 1.

`src` is `gateway`, `dashboard`, `patrol`, or one of the two guest forms `crew:<name>` and `app:<name>`. A guest is an emitter that names an INSTANCE rather than a subsystem, and a crew's log is the one kind that accepts one, because it is the record several units contribute to: a child crew reports into its parent's crew log, a conductor signs its own dispatches, and an app records a fact no built-in domain covers. Section 6 states what each guest may write.

The families, from the RFC:

| family | types | pointer |
|---|---|---|
| shipped | `member/*`, `activity/record`, `slot/*`, `patrol/*` | `slot/*` -> the session |
| messages | `message/received`, `message/sent` | redacted body in the crew log |
| tree | `crew/child-attached`, `crew/parent-attached`, `*-detached` | -- |
| dispatch | `crew/dispatch`, `crew/report` | report -> the child's segment |
| topics | `crew/topic-*`, `crew/forwarded`, `crew/run-state` | topic -> its work session |
| items | `item/phase {from,to,reason}`, `item/next`, `item/probe`, `item/verdict`, `crew/round-*` | probe, verdict -> evidence |
| knowledge | `crew/finding`, `crew/summary`, `crew/note-*`, `crew/link` | the segment covered |
| memory | `memory/bound\|copied\|forgotten\|restored` | -- |

Two of these families carry a contract with REQUIRED fields. **Required here is a contract on the writer, and what enforces it is a declaration rather than a branch:** a per-type `data` requirement belongs to the type registry, next to that type's own `data` shape, not to `check_ownership`, which answers who may write a type rather than what the type must contain. Until that module exists these two contracts are held by review against this section. TODO: declare them in the crew log type registry module, `kiro_crew.crew_log.types`, when it lands.

**`crew/dispatch`** -- a parent asking for an item to be worked.

```json
{"type":"crew/dispatch","seq":120,"time":1789000000000,"src":"crew:conductor",
 "data":{"item":"pr-4127","target":{"kind":"session","slot":"dashboard:3"},"brief":"drive it green"}}
```

| Field | Required | Meaning |
|---|---|---|
| `data.item` | yes | The item this dispatch is about. |
| `data.target` | yes | `{kind: "session", slot}` or `{kind: "crew", name}`. |
| `data.brief` | no | What the target is being asked to do. |

`target` is required because a dispatch with no target names nobody. It is not a hint a reader can fill in later: the `board` projection folds dispatches into who owes what, so an entry naming no one is a row the fold cannot place, in a file nothing rewrites.

**`crew/report`** -- the answer to a dispatch.

```json
{"type":"crew/report","seq":388,"time":1789000000000,"src":"crew:qa","thread":120,
 "ref":{"unit":"session","id":"s-7f3a","from":40,"to":96},
 "data":{"item":"pr-4127","status":"done","credits":0.21,"summary":"merged"}}
```

| Field | Required | Meaning |
|---|---|---|
| `data.item` | yes | The item being reported on. |
| `data.status` | yes | One of `done`, `blocked`, `failed`, `progress`. |
| `data.credits` | no | Credits the child spent. Absent is not zero. |
| `data.summary` | no | One sentence. |
| `ref` | yes | A segment of the child's crew log: the evidence. |
| `thread` | when replying | The dispatch's `seq`. |

`ref` is required for the same reason `target` is. A report is a CLAIM about work that happened somewhere else, and `board` and `budget` fold status and credits straight off it without opening the child's crew log; the `ref` is what makes that fold checkable rather than trusted. A report with no `ref` is an unfalsifiable claim, permanently, since nothing later can attach the evidence to a line that is already written.

`thread` is the dispatch's `seq` when the report answers one, which is what makes a dispatch and its replies one conversation inside the parent's file. A report volunteered with no dispatch behind it carries no `thread`.

Both of these are one type each, not one per writer. The child's identity is `src`, so two children reporting on one item write the same `type` into one file and are told apart by who signed them.

`ref` on a report is **the one cross-kind bridge a writer takes**: from a crew's log into a session's segment, one level down, in the direction a conductor reads. The pair is symmetric with `subagent/spawned` (section 5): the child's header `thread` points up at the entry that caused it, and that entry's `ref` points down into the child's record.

## 5. The session-log format, pre-release

**The shapes below are PRE-RELEASE and may change.** `KIROCREW_CREW_LOG` defaults off, so
no crew log directory is created on a stock install and there is no user data on disk for a shape
change to break. While that holds, a type may be added, removed or reshaped in one commit.

**The freeze point is the release that turns the flag on by default.** From then on there are
files a reader may hold, so the compatibility strategy has to be decided rather than assumed: a
type gains fields additively and an unknown type is skipped when its writer marked it
`ignorable`, OR a shape change carries a migration. That choice belongs to the change that flips
the default, which is the first one with data to migrate. `version` is the escape hatch it would
spend.

Every type is `domain/<past participle>`, a fact that happened. Every turn-scoped entry carries
`data.turn`, and `data.step` where a step exists. `thread` stays unset on session entries.

The **Emitter** column says what exists in this commit. `yes` means the gateway writes it today;
`—` means the type is owned, writable and specified, and nothing writes it yet. A type with no
emitter is kept only where the site that will write it is identified; the types with no honest
source were removed (see the emitter spec's "Removed types").

### Session, turn
| Type | `data` | Emitter |
|---|---|---|
| `session/opened` | header echo + `resumed`; `model_requested` when a tier resolved one; `parent {slot, sid?}` on a session another session made through `session_create` | yes |
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
| `request/configured` | `{turn, model, provider, context_window, system?, system_bytes?}` — written on change only | yes |
| `context/composed` | `{turn, step, sources:[{kind, chars, tokens}], chars, tokens, tokens_estimated}` | yes |
| `step/started` | `{turn, step}` — one model call | yes |
| `step/completed` | `{turn, step, ms}` | yes |

`context/composed` is where the per-turn bill for what the gateway injects lands: `sources` names
every block put in front of the model — `system`, `memory`, `lessons`, `skills_index`, `steering`,
`project`, `tool_specs`, `ledger_context` — each with its `tokens`.

### Tool, approval

| Type | `data` | Emitter |
|---|---|---|
| `tool/called` | `{turn, step, call_id, name, server, kind, args_hash?, args_bytes?}` | yes |
| `tool/completed` | `{turn, step, call_id, status, is_error?, elapsed_ms, result_hash?, result_bytes?}` | yes |
| `approval/requested` | `{turn, approval_id, tool?, reason?}` | yes |
| `approval/decided` | `{turn, approval_id, decision, by?, cause?}` — `decision: "unknown"` is written only by the interrupted-tail repair | yes |

Arguments and results are DIGESTED, never recorded: `args_hash` / `result_hash` are the sha256 of
the serialized payload and `args_bytes` / `result_bytes` its length. That answers "same arguments as
last time" and "how large was this" without the crew log becoming where a shell command's secrets and
a file's contents accumulate. Bodies, if they ever land, arrive as their own change. Each pair is
absent rather than zeroed when there is nothing to digest, since 0 is a real size. `is_error` is
tri-state and absent when the caller did not say, because "nobody asserted this worked" is not the
same claim as "it worked".

Approvals ARE emitted. An earlier revision said they could not be, because "the approval coordinator
carries a slot key, not a session id" -- true of that class, and false of the site that actually
raises the prompt, which sits inside the runner's turn where the session id and the turn ordinal have
been in scope since the turn began. `reason` is the redacted title the human is shown. `by` and
`cause` are written only for a decline the HOST made, which is the one decision this site can
attribute: an answer that came back from a person could have arrived at the dashboard or in Slack,
and naming a guess is worse than naming nobody.

### Model, compaction, plan

| Type | `data` | Emitter |
|---|---|---|
| `model/selected` | `{turn?, model, source}` | yes |
| `compaction/applied` | `{turn, pct_before, pct_after, freed_pct}` | yes |
| `plan/updated` | `{turn, items:[{id, text, state: done \| open}], total?}` — the session's own task list | yes |

### Ledger

| Type | `data` | Emitter |
|---|---|---|
| `ledger/recorded` | `{slot, goal?, phase?, next?, tried?:{approach, rejected_because?}, artifacts?, event?, event_kind?}` — one session-ledger update | yes |

One entry per `session_ledger_record` call, carrying only the fields that call set;
an absent field means unchanged. A phase and the event explaining it ride on the
same entry, so no reader can observe a phase that moved without its reason. No
`turn`: a slot records against its workstream rather than against whichever turn it
happened to be inside, and the record is folded across every session unit of the
slot (`session-work-ledger.md`).

### Background and children

| Type | `data` | Emitter |
|---|---|---|
| `background/completed` | `{kind: title \| memory_consolidation \| summary, model?, provider?, tokens?, credits?, ms?}` | yes |
| `subagent/spawned` | `{turn?, agent_id, agent?, model?, scope:{memory, lessons, project}}` — no `ref` yet, see below | yes |
| `subagent/steered` | `{agent_id, mode: interrupt \| follow_up}` | yes |
| `subagent/completed` | `{agent_id, ms?}` — no `tokens`/`credits`, see below | yes |
| `subagent/failed` | `{agent_id, reason?, outcome: failed \| stopped \| unknown, ms?}` — `unknown` is written only by the interrupted-tail repair | yes |

These are the families a single-agent runtime never needs and a gateway does: every token spent on a
session's behalf, whether a person asked for it or not, is a fact in that session's log attributed to
what caused it.

A subagent WOULD be a session with its own crew log, whose header `thread` points at the parent's
`subagent/spawned` entry while that entry carries a `ref` into the child's log -- the same pair as a
crew dispatch, one level down. It is not one yet: the only site that creates a session log is the
dashboard turn path, and a subagent run does not go through it. So the child's facts are written into
the PARENT's log, `subagent/spawned` carries no `ref`, and the pair above becomes writable, unchanged,
the day subagent sessions get logs of their own. Citing a child file that does not exist would be
indistinguishable, to a reader, from citing one that was deleted.

The child's spend is likewise absent rather than zeroed. Nothing in the subagent runtime measures
tokens or credits -- a run's record carries elapsed time and peak resource use, and the child never
reports its spend back to the parent -- so `subagent/completed` writes neither. Background calls are
the opposite case and DO carry both, because the two background entry points already measure them for
the usage store. `digest` is not among their kinds: the design named it, and the digest path turned
out to be a filesystem read with no model call in it.

The edge that IS written today is the `session_create` one, and it takes the child's side of that
pair only: a created session's `session/opened` carries `parent {slot, sid?}` -- the creator's slot
key, and the creator's ACP session id as `session_create` froze it when it minted the child. It is
recorded on the child because the child is the side that knows it: `_created_by` and `_created_by_sid`
are stamped on the slot at mint, before any turn, while the creator never learns the child's id. The
sid is frozen rather than read at the child's first turn because a creator slot can be closed and
replaced in between, and a live read would cite the replacement's crew log in an entry that is never
rewritten. The edge is written only from a mint THIS gateway process witnessed (`_lineage_minted`,
never persisted): the transcript is a file an agent's file tools can edit, and this log is fenced from
those tools precisely so nothing in it can be forged as gateway-authored, so `_created_by` read back
from transcript metadata is restored for authorization but never promoted to lineage, and the sid is
not persisted at all. A child whose gateway restarted between mint and its first turn therefore
writes no `parent`. `sid` is a citation of the creator's unit, not the tree key -- a slot outlives its ACP
session, so the fold that builds the session tree (`crew_log/tree.py`, `crew-log-projection.md` section 6)
keys it by `slot`, reads the first `session/opened` of each crew log, takes the parent from any log of
the slot that carries one, and never lets a log without one retract it.

## 6. Rules

Every refusal is a `CrewLogError` carrying a stable `code`; the codes are API surface and are additive-only.

**Ownership** answers whether a kind of unit has such events at all. `schema.TYPE_OWNERSHIP` maps kind to owned `type` domains -- crew: `member` `activity` `slot` `patrol` `message` `crew` `item` `memory`; session: `session` `turn` `step` `tool` `approval` `model` `compaction` `plan` `ledger` `message` `request` `context` `background` `subagent` `write` -- and anything else is `event_type_not_owned`. It is prefix-based, so a new action under an owned domain needs no change: `crew/dispatch` and `crew/report` are owned by the `crew` domain the registry already lists. `message` appears in both registries, which is what ownership means: a crew forwards messages and a session records its own bodies, so both kinds have such events and neither name is a collision.

**Namespacing** answers whether an emitter may write it, and it is a rule about `src`. Two halves:

- **Which emitters a kind takes at all.** `schema.KIND_FIXED_SOURCES` and `schema.KIND_SOURCE_PREFIXES` are the lists, spelled out in 4a and 4b; anything else is `bad_src`. The lists are per kind rather than shared because the writers are: a shared list accepts `patrol` inside one session's own turn history, and `src` is what a reader attributes an entry to, so that is an authorization hole rather than a convenience. `require_src` therefore takes `kind` as a required keyword argument -- it selects the rule, so a caller that omits it fails loudly instead of having its `src` measured against some default kind's list.
- **What a guest may write.** A `crew:<name>` emitter writes the crew kind's own built-in domains: its name in `src` is the signature, so `crew/report` is one type every child writes and the entries are told apart by who signed them. An `app:<name>` emitter writes only under its own `app:<name>/` type prefix, else `namespace_violation`. That prefix is the ONE guest type namespace, kept for a fact no built-in domain covers, and it is judged by this rule *instead of* ownership -- which is why the registry needs no app entries.

A type never carries the writer's identity. `crew:<name>/<action>` is not a type at all but a malformed one (`bad_type`): identity belongs in `src`, where authorization reads it, and a type that repeats it would make the same fact a different type per writer -- so a fold would need to parse the type to group two children's reports on one item, and the registry would grow an entry per crew.

What this layer does NOT check is any relationship between the guest and the crew log. It has no crew tree to consult, so it cannot ask whether `crew:qa` is really a child of the crew whose file it is writing into -- and a check with nothing behind it only looks like a boundary, the same reason `resolve` has no `forbidden` status yet. Authorization here is that the emitter is a form this kind accepts and that a guest stays inside its own namespace; who is whose child arrives with the grants, at the layer that has the tree.

The remaining bounds: `thread` must name an existing, parseable, earlier seq (`bad_thread`); `ref` must be well-formed and span at most `MAX_REF_SPAN` lines (`bad_ref`); a serialized entry must be at most `MAX_ENTRY_BYTES` (`entry_too_large`). Caps refuse rather than truncate, leaving the file byte-identical -- a clipped record the caller believes landed intact is a loss the caller cannot detect.

**An unknown type is the reader's rule, and the writer declares the exception.** `iter_from(known=...)` is a reader stating the types it can interpret; without that argument nothing changes and every entry is yielded, which is what every caller predating the marker gets. With it, an entry whose type is not in the set is skipped when the writer marked it `ignorable: true`, and raises `unknown_entry_type` when it did not. The asymmetry is the point: skipping an unknown KEY loses a detail, while skipping an unknown LINE can lose the plot, because a required entry a reader cannot interpret may change the meaning of every entry after it. A fold that continued past one would return a confident wrong answer instead of an admitted failure, so the refusal names the seq it stopped at and the reader can resume there once it learns the type.

Only the writer can make that promise, since only it knows whether the entry samples a stream or states a fact -- so the marker rides on `append`, never on the read. Read-back is strict: a literal `true` sets it and anything else reads as absent. The marker RELAXES a guard, and this tree is agent-writable, so a truthy coercion would let a damaged or planted line switch the guard off with a string or a number.

The gate is on `iter_from` alone. `page` and `resolve` render history for a person or drill into a citation, where displaying an unfamiliar line is a missing detail rather than a corrupted fold, so neither takes a vocabulary and neither refuses.

## 7. Append-only guarantee and damage

A line is never rewritten. Exactly one mutation exists: on `open`, trailing bytes that are not a complete line are dropped. Termination decides which those are, so the rule needs no heuristic. Every append writes `line + "\n"` and fsyncs, so unterminated bytes that fail to parse are a crash artifact and go; unterminated bytes that *do* parse lost only their newline, so the record stays and the next append re-supplies the separator. A terminated line that does not parse is damage inside history: reads skip it, the file keeps it. Two readers of the same bytes therefore always agree.

A failing write ROLLS ITSELF BACK. Bytes reach the file before the fsync runs, so a failure anywhere in the write-then-sync sequence leaves an outcome nobody knows: the entry may well be durable. That matters because the write-behind retains and retries a failure, and a retry against an unknown outcome appends the same fact a second time under a new seq -- a duplicate no reader can tell from a real repeat, in a file nothing rewrites. So the append truncates the file back to the size it had before the attempt and re-raises, which makes the failure definite: either the append is whole and synced, or it is gone and the retry writes it cleanly. A partial write is removed for the same reason, and one more -- leaving half a line behind would defer the cleanup to the next append's torn-tail check, so the file would carry a fragment until then.

That truncation is not a second exception to the never-rewrite rule. The bytes removed are the caller's own failed append, so the file is restored to a state a reader could already have seen rather than edited; it is the torn-tail rule applied at the moment the tear happens instead of at the next open. It is safe against a concurrent writer because the append lock is held across the write and its rollback, so no other handle's entries can lie inside the range.

If the rollback ALSO fails -- likely, since whatever broke the write is often still broken -- the file may hold bytes no entry claims, and `IndeterminateAppend` reports exactly that. The residue is an unterminated or unparseable tail, which is the shape the next `open` truncates, so the recovery already exists; the distinct type is so a caller can tell "nothing happened" from "something may have".

The header is the exception, and only at creation: `CrewLog.create()` publishes it with `atomic_write` (temp file, fsync, rename), so the file is either complete or absent and a crash or ENOSPC mid-header cannot leave a fragment. Without that, a fragment would be read as a torn tail, truncated to an empty file, and then refused by `open` while `create` refused the very file it had produced -- a unit wedged with no automated recovery. For the same reason all three of `create`, `open` and `exists` read a **zero-byte file as absent**: it carries no header and no entries, so there is nothing to protect and one shared meaning is what keeps the paths from disagreeing. After the rename the file is append-only for the rest of its life.

Decoding is **strict and per line**, on bytes read in binary mode. Replacement-decoding would be the wrong tolerance: invalid bytes inside a JSON string can decode into still-valid JSON, so a damaged line would be yielded with `U+FFFD` substituted into its values rather than skipped -- handing a consumer altered data as authority, which a record that calls itself the authority must never do. Byte damage is this machinery's expected input, so an undecodable line is damage and is skipped exactly like unparseable JSON; an undecodable header is `bad_header`.

Both reads are FRAMED by `jsonl_util`, so one planted line cannot cost more memory than the format's own write limit (`MAX_ENTRY_BYTES`) however long it is -- the tree is agent-writable, so an unbounded read is a memory bound an attacker chooses. The two use different postures on purpose. Entries take the SKIP posture (`bounded_raw_records`), matching the rule above: an over-cap line is not something this writer produced, so it is damage and costs that one line. The header takes the ABORT posture (`strict_raw_records`), because skipping an over-cap line 1 would hand back line 2 -- an entry -- as though it were the header. That is a wrong answer rather than a missing one, so an unreadable line 1 becomes `bad_header` and the unit's identity fails closed.

### Interrupted turns

A header naming a format version this build does not read is refused BEFORE the rest of the header is checked and before any row is decoded, with its own code (`unsupported_version`) and a message telling the reader to upgrade and stating the file is not damaged. The ordering is the point: a newer format need not satisfy any of this build's structural checks, so its new required type would surface as `unknown_entry_type` and its header shape as `bad_header` -- both of which accuse the file of corruption when the truth is that the reader is old, and someone acting on that diagnosis deletes a healthy log. Refusing NEWER only; an older version opens as it stands, which is what a future migration is for. Nothing else in this module reads `version`, so without this check carrying it buys nothing.

A session log can be left holding an opener open by a crash, a `SIGKILL` or a pod eviction. `CrewLog.open(kind, id, repair=True)` -- or `log.repair_interrupted_turn()` on a handle already held -- closes what is unbalanced, appending deterministic closers: an `approval/decided` with `decision: "unknown"` for each unmatched `approval/requested` inside the open turn, then a `tool/completed` with `status: "unknown"` for each unmatched `tool/called` inside it, then a `subagent/failed` with `outcome: "unknown"` for each unmatched `subagent/spawned`, each group in first-seen order, and finally `turn/completed` with `stop_reason: "interrupted"`. That order is the only one a live writer could have produced: an approval is decided before the call it gates completes, and a turn cannot close while a call inside it is open.

Two of those three are TURN-SCOPED and the third deliberately is not. A call completes within its turn, and an approval is decided in the same `finally` the turn's own path runs through, so an unmatched one is collected only from inside the open turn -- an unmatched call or approval in a turn that DID complete is a different anomaly, and inventing an outcome for it here would be the repair editing history it was not asked about. A child is the opposite: `subagent/spawned` is matched across the WHOLE file, because a subagent's whole point is that it starts, steers and reports long after its asking turn ended. Scoping its closer to the open turn would close only the rare child that died inside its own turn and leave every ordinary one open forever. It follows that closing a child can fire on a file whose newest turn completed normally, and in that case NO `turn/completed` is appended -- that turn closed itself, and a second closer would be a turn closing twice.

The child closer needs a STRONGER precondition than the other two, and it is the caller's live subagent registry rather than any fact about the file. What a repair knows is that the WRITER is gone, which settles a turn-scoped opener because the turn died with its writer. It does not settle a child: a child outlives its asking turn by design, so a writer torn down inside a live process can leave one still running and still able to file its own real terminal, and a closer written here would put two outcomes for one `agent_id` in a file nothing rewrites. An unmatched opener is what a finished-but-unreported child and a still-running one both look like, so no flag can stand in for the answer -- including "a resume", which is raised for an in-process `session/load` too and therefore does not even imply a different writer. `CrewLog.open` and `repair_interrupted_turn` take `child_gone`, a predicate over `agent_id` that the gateway backs with its subagent manager; omitted, or raising, no child is closed. What backs it has to live for the PROCESS, not for a session: the manager's run map is pruned only when a run COMPLETES, so it still lists a running child across the idle teardown and session reset that clear the emitter's own per-session maps -- which is the case that makes a session-scoped source report a live child as finished. A source that is also bounded, and so can evict a live entry under load, is wrong for the same reason. The registry answers whether a child is still RUNNING, which is not the question of whether its outcome is RECORDED: a terminal outcome reaches the file through the writer and the emitter's submit returns before the entry lands, so a child that has just reported leaves the running set while its own closer is still owed -- and being done, its run record is by then inside the reach of the completed-run bound as well. So the emitter refuses to report any child of a session gone while it still owes an entry for THAT session. The debt set answers this exactly and carries no such bound: it holds a queued entry, one the writer has CLAIMED out of the queue and not yet attempted, a retained one, and an owed loss marker alike. The claimed case is the one a reader would miss on their own, because claiming a session's entries takes them out of the queue while they are still owed. The entry being appended at this instant is deliberately NOT among them: the caller asking is itself a job of that batch, and a job that counted itself would refuse every child on the resume path this repair exists for. An entry a hard ceiling refuses is absent from it, which is the wanted answer, because that closer is never coming and the opener is the repair's to close. That default is the direction that loses nothing: a reader treating an open child as unknown is behind, while a fabricated outcome in an append-only file cannot be corrected.

`"unknown"` is the same word in all three, and it is written by the repair alone. A live writer always knows what a human decided and how a child ended, so a reader that sees it knows it is reading a reconstruction rather than an observation.

A group whose members are meaningless apart is written by `append_many`, which takes the file lock once, allocates a contiguous seq run from one read of the tail, and writes every line with one `write()` and one `fsync`. The one caller today is a body too large for a single line: the `message/chunk` entries plus the entry that CITES their seqs. The citation is built INSIDE the lock, by a `cite(seqs)` callable the caller passes alongside the chunks and which receives the run the call has just allocated, so a citation cannot disagree with the allocation it names. Allocating outside the lock and citing the result cannot be made correct, because the lock is a cross-process one: between that read and the write another handle can append and shift the run, after which `chunks` name the intruder's entries -- seqs that exist and parse, so no later read can tell the body apart from the right one. Appending the members separately leaves a different window, in which a hard kill puts the body on disk with nothing pointing at it -- stored and unreachable, with no record of the message at all. Every refusal still happens before any byte is written, including validation of what `cite` returns, so a rejected group leaves the file identical, and `thread` is not accepted because a group is self-contained and an anchor check would have to re-read the tail it is being allocated from.

One write can still tear, so the repair handles what a torn group leaves. A TRAILING run of `message/chunk` entries with no citing entry after it is unreachable, and `repair=True` truncates it -- the same allowed mutation as a torn tail -- with one warning naming the seq range. The residual is the one message that was mid-write, which is the residual of any single entry lost to a hard kill, and the freed seqs are reused by the repair's own closers so a fold sees a contiguous run rather than a gap. A chunk group followed by any other entry landed whole and is left alone.

That scan reads with the ABORT posture, because the offset it produces feeds a truncation. A bounded read drops an over-cap record in full, terminator included, without yielding it, so a byte walk over what such a read yields stops matching the file at the first oversized record and every offset after it is short by that record's length -- and truncating at a short offset cuts valid entries. So a record that cannot be delivered intact aborts the scan and nothing is truncated, with a warning naming why. The cost of failing closed is one unreachable body left in the file, against losing history that was never in question.

**Repair is opt-in, and only a RESUME may ask for it.** A plain `open` never mutates the record. The two callers of `open` want opposite things: a resume -- the gateway finding a session's log whose writer is gone -- wants the open turn closed, while a live writer RECONNECTING to its own crew log must not have it closed, because its turn is still running and a `turn/completed {interrupted}` landing mid-turn would claim an outcome the turn never had and then be followed by more of that turn's entries. Nothing in the file distinguishes an open turn from a dead one, so only the caller's situation can: repair cannot be inferred from the fact that an `open` is happening. A reconnect happens for reasons unrelated to the writer's health -- a handle dropped from a bounded cache is enough -- which is precisely how an unconditional repair corrupts a live turn. The caller's situation is a belief about a writer it cannot see, which is why it is not the only check: see "Write ownership" below.

Every closer reuses the LAST REAL entry's `time`. A closer describes what happened when the writer stopped, not when some later process opened the file, so the current clock would put a gap of arbitrary length inside a turn and make any duration computed off these entries a measure of downtime. Reusing the time also makes the repair deterministic: the same bytes in produce the same bytes out, whenever it runs, and a second repair adds nothing because the tail is balanced.

This is still append-only -- nothing is rewritten and `seq` continues -- so a reader that already folded the file sees only new lines. It is scoped to the SESSION kind, whose turn lifecycle these types belong to, and to the OPEN turn: an unmatched call inside a turn that did complete is a different anomaly, and inventing a result for it would be the reader editing history it was not asked about. Best-effort, like the torn-tail repair: a closer that cannot be written leaves the tail open, which is the state every reader already tolerates.

**A content-aware repair reads with the abort posture, because its result decides whether history is added.** The ordinary reader skips a malformed interior line so one damaged line cannot hide the history in front of it, and that is the wrong tolerance here: a turn whose real `turn/completed` is byte-damaged reads as still open once the line is skipped, and a closer appended from that fold states an outcome no writer observed -- permanently, since later folds skip the damaged real completion too and see only the fabrication. So the fold reports every record it could not read, and a skip refuses the repair outright: the file is left exactly as it is with one warning naming the record. This is the same rule the truncation-offset scan already follows, and both share one classification of what counts as a record, because their results feed the same mutation -- one chooses where the file is cut, the other whether lines are added to it.

`seq` is read back from a bounded window at the file's end inside the per-file lock rather than trusted from an in-process cache, so two writers cannot both claim one number and the read costs the same on a ten-line crew log or a million-line one. `store._anchor_exists()` reuses that same window, so proving a `thread` anchor is parseable is free for a recent anchor and falls back to a scan only for one older than the window.

`resolve` has four outcomes and takes NO access callback. `ok` is a complete answer; `gone` means the cited unit has no crew log at all; `pruned` means the span reaches BELOW the oldest surviving segment's first seq, so retention removed it; `corrupt` means the span lies inside a segment that still exists yet read short, which is damage.

**Retention and damage are never reported as each other.** Both look identical from the caller's side -- a short answer -- so the classification is made from the segment names rather than the result: a span starting below `segment_first_seqs()[0]` is `pruned` when the part of it at or above that seq reads back whole, and anything else that reads short is `corrupt`. The surviving part is checked separately because a partly pruned span is short for a legitimate reason -- so the count that would catch damage in it is already satisfied -- and a citation whose head retention removed can still hold a damaged line further up, in a segment that is still on disk. Reporting that as retention hides the damage behind a normal answer. Getting this backwards is worse than either error alone. A reader told `pruned` stops looking, because retention removing old lines is a normal answer; told `corrupt`, it knows the file it still has is not intact. Reporting damage as retention therefore converts a recoverable alarm into silence. Citing PAST the newest entry is `corrupt` too, and deliberately not `ok`: a `Ref` always names a definite span -- absent `to_seq` means the single line at `from_seq`, so there is no open-ended spelling -- which makes a span reaching beyond the newest entry a claim about lines that are not in the file. `ok` means every cited seq was read back, so a short answer is the citation failing whatever shortened the file. That also covers the case with no torn bytes to give it away: whole lines cleanly truncated lower both the walked entries and a reopened handle's tail together, and an expected count taken from the file rather than from the citation would shrink to match what survived and answer `ok` with the cited lines gone.

This layer claims no authorization, so it has none to deny: a check defaulting to allow would make the shortest call shape the insecure one, and a check with no permission model behind it only looks like a boundary. Every caller today is in-process gateway code that can already read the file. A `forbidden` status arrives with the first caller that HAS a permission model -- the routes that mount this -- where the caller identity it must be derived from actually exists.

### Write ownership

A resume's belief that the previous writer is gone is not verifiable from the file, so it is not the only check. **Writes to a unit are owned, and the arbiter is the kernel.** The owner holds a non-blocking advisory lock on a `.lease` file beside the log, and a process that cannot take it is REFUSED with `already_owned` rather than made to wait: it appends nothing and repairs nothing, so the file keeps one writer's account of a turn instead of two interleaved ones. This is what stands between a resume in a second gateway and a turn that reads as completed-as-interrupted and then, further down, completed for real, with one tool call closed both `unknown` and `completed` -- a shape a fold cannot resolve and no later pass can undo.

Ownership is taken LAZILY, on a handle's first write, and never by `open` itself, because `open` also serves readers: `iter_from`, `page` and `resolve` need no ownership, and making a reader contend with the writer would buy nothing. `open(repair=True)` claims it before the closers, and that is the same rule rather than an exception -- the closers are appends. `create` claims nothing: it publishes a header for a unit that has none, and two processes racing it are already settled by `already_exists` under the per-append lock.

The lock is REFCOUNTED PER PROCESS, keyed by the lease file's path. That is a correctness requirement rather than an optimization: a POSIX lock belongs to an open file description rather than to a process, so a second `open()` of the lease path inside one process contends exactly as another process would -- and one process legitimately holds several handles for one unit, since the emitter's cached handle and the handle a session claim opens overlap while the cache entry is replaced. So the first writer in a process takes the kernel lock, every later handle shares it, and the last handle to be dropped gives it up. The path is the key rather than `(kind, id)` because the data home is repointable and the kernel locks a file, not a name. Acquire and release both run under one module lock, for the same reason the count exists: two threads reaching for one unit must share a descriptor rather than race two of them and have one refuse the other.

Release is bound to the HANDLE being dropped rather than to an explicit call, and that timing closes the window from both sides. The emitter's eviction rule never drops a handle belonging to a live turn, so ownership can only end BETWEEN turns -- and between turns there is no live turn for a successor's repair to damage. A terminal event is queued rather than written, so "between turns" begins when that entry LANDS, not when it is handed over: the emitter marks the turn's record as owing a closer at handover, and a re-claim leaves such a record alone while still closing one whose terminal was never emitted, since nothing else will ever close that one. Meanwhile a queued write that still holds the handle keeps the ownership it is about to need, which an eager release at eviction would have taken out from under it.

There is deliberately NO expiry. The case ownership is for needs none: a crashed, killed or evicted owner has its lock dropped by the kernel when its descriptor closes, so a successor takes ownership at once and closes the turn its predecessor left open. What an expiry would add is the power to expropriate a writer that is merely SLOW, whose next append then lands after a successor has already closed its turn -- the exact damage this prevents, reintroduced as a timeout. A live but wedged owner therefore keeps ownership until its process exits, and the claim it blocks reports its own loss instead of taking the log.

After locking, the held inode is compared with the file now at the lease path, and a mismatch starts over. A POSIX lock names an inode, so a lock on a path that was unlinked and recreated proves nothing about the file a writer is about to append to. Nothing in this tree removes a LIVE unit's lease file: the one path that removes one -- `remove_unit`, under "Retention: whole units" below -- first takes ownership no other handle shares, so by then the unit has no writer, and it unlinks the lease last, after every other file is already gone. The check is what makes that a property of the code rather than an assumption about it.

## 8. Retention: segment files

A crew log is one or more SEGMENT files. `log.jsonl` is the segment beginning at seq 1; a later
segment is `log.<first_seq>.jsonl`, with its first seq in the name so ordering needs no file read.
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
**No writer rotates yet** -- a writer appends to the newest segment, which is `log.jsonl` in every
crew log today. What creates the second segment, and on what trigger, is a later change that needs no
format change to land.

### Retention: whole units

A unit's whole crew log is removed by `store.remove_unit(kind, id)`, and that is the ONE spelling of
deletion in this module: the retention sweep and the session permanent-delete funnel both call it,
because two callers deleting one tree two ways is two chances to get the order wrong and the order is
the entire correctness argument. It is not rotation and not a format change, and NOTHING is written
to a crew log that is about to go -- no tombstone, no `pruned` entry. A reader holding a citation into
it already has its answer: `resolve` reports `gone` for a pointer into a unit with no crew log at all.

**Removal goes through the lease, and the lease it takes is SOLE.** Ownership is what stands between
a removal and unlinking the segments a live writer is appending to, so the removal claims it
non-blocking and is refused with `already_owned` when it cannot -- the unit is live, that pass does
nothing, and a later one collects it once its writer is gone. `sole` is load bearing rather than
decorative: the lock is refcounted per process, so a plain claim against a unit THIS process is
already writing succeeds by joining that count and proves nothing, which would let a sweep running
inside the gateway delete the crew log of a session the emitter's cached handle is mid-append to. A
sole holder also blocks every later claim, shared or not, until it is released; without that the
refusal is one-directional -- a writer arriving second would join the remover's own lock and append
into a unit whose files are being unlinked.

**Then the caller RE-DECIDES, inside the hold.** A `guard` callback is REQUIRED and is called once
ownership is held; the unit is removed only if it answers true. The lease alone is not enough, and the
gap is the one `purge_matching` already documents: a selection made outside it is a SNAPSHOT, and
ownership deliberately ends BETWEEN turns, so a session can be revived, append, finish its turn and
release the lease in the window between the decision and the delete -- after which the removal would
take a live conversation's log while contending with nobody. Re-reading under the hold is what makes
the decision current, which is why the guard is a callback rather than a filter the caller applies
first, and why there is no default that skips it. The sweep's guard re-derives the same expiry answer
and requires the same unit id; a caller whose reason is not a property of the file passes an
accept-all guard and says at its call site what does decide.

Then order, with IDENTITY LAST. Segments carry the header, so they are the history and they go first;
the per-append `.lock` next; then any other entry, none of them followed if it is a link. The
`.lease` file is removed LAST and only by its holder, which is what keeps the inode check under
"Write ownership" a fact about this code rather than an assumption: while the lease exists its path
names the file whose lock proves ownership, and a lease unlinked before the segments would let a
second remover take a lock on a fresh inode at the same path and unlink the same files concurrently.
Windows refuses an in-hold unlink and gets it after release instead, which is safe there precisely
because it fails while any handle is open -- the same asymmetry `session_ledger.unlink_lock_in_hold`
documents, and that function is reused rather than copied. Failures are COUNTED and reported for what
they are: a unit that could not be fully removed answers `failed` rather than being reported as
collected, and stays addressable by the next pass -- but `failed` does not promise its history
survived. Segments go FIRST, so the ordinary partial removal is history already gone with something
else left standing, and the log line distinguishes that from a removal that got nowhere. Claiming the
segments were kept would send a reader looking for a record the pass had destroyed.

A unit directory that is a LINK is refused, and the check is on the name as WRITTEN. `crew_log_dir`
returns the RESOLVED path, so a link pointing at another unit stays inside the root, satisfies
containment, and hands the removal its target -- which is not itself a link, so checking the resolved
path would prove nothing and one unit's id would delete another unit's history.

**What the sweep selects.** `store.sweep_expired(days)` is called by
`history._cleanup_old_archives`, so `session.archive_retention_days` governs crew logs too: one switch,
one hourly throttle, no new config key, and a negative value disables both halves. It walks
`crew-log/sessions` alone -- the crews' logs are never in scope, since they have no writer and no
`session/closed` to age from, and a rule invented for them now would be a guess applied to files
nothing produces. A missing root costs one directory listing, which is also what makes the sweep de
facto gated by `KIROCREW_CREW_LOG` without reading it: only the emitter creates session units.
Reading the flag here would be worse than not reading it, because turning it off would then strand
every crew log already written, permanently.

A unit is EXPIRED when its newest LIFECYCLE entry is a `session/closed` older than the cutoff.
`session/opened` and `session/closed` are the pair that moves a unit between open and closed, and the
newest of the PAIR is what decides -- not the newest close on its own. A resumed session appends to
the crew log it already had, so `... closed ... opened ...` is a legitimate file whose session is
running right now, and a rule that read the newest close would call it expired and delete a live
conversation's log. Entries that are neither -- a turn, a tool, an in-flight closer the emitter writes
after a teardown by design -- say nothing about the state and are skipped.

Four things are skipped regardless of age, and each is a refusal rather than an oversight:

- **An OPEN unit** -- one whose newest lifecycle entry is a `session/opened`, or which has no
  lifecycle entry in the window at all. The deciding entry is looked for in a bounded read of the
  newest segment's end, and only entries written after it can push it out; once a unit is closed the
  emitter writes nothing but a handful of in-flight closers unless the session is revived, which
  appends its own `session/opened`. So a lifecycle entry outside the window means a live session --
  exactly the unit that must be kept -- and the bound fails closed instead of scanning every crew log on
  every pass.
- **A torn tail.** Unterminated trailing bytes are what `open(repair=True)` truncates, and the sweep
  cannot tell a dead writer's crash artifact from an append that has not reached its fsync -- the
  bytes are identical. Deleting the unit would destroy the history the repair exists to recover.
- **A header whose id does not fold back to its own directory name.** The removal is aimed by id, so
  a directory carrying another unit's id would have the removal land on that other unit.
- **A close whose reason does not END the ACP id's life.** A unit is collectable on exactly ONE reason:
  `destroyed`, and that word asserts REVOCATION COMPLETED rather than "a destroy ran". `destroy` removes
  exactly one map key, so before claiming it the teardown checks that no OTHER key still maps to that
  sid; when one does, it records `destroyed_sid_retained` instead and the log stays. Two keys on one sid
  is a state the system itself produces -- importing a transferred session twice allocates a new slot key
  each time and deliberately leaves the source intact (`dashboard/session_transfer.py`) -- so the other
  holder is NOT revoked to make the record tidy, and its shared log is not collected while it can resume.
  An unreadable map withholds the claim for the same reason. Reading the map to WITHHOLD a claim is safe
  in the way reading it to grant one is not: a forged or emptied map can only make the gateway assert
  less than the truth, and the sweep itself still reads nothing but the crew log. The permanent-delete
  funnel applies the same veto, because it deletes without consulting the reason.

  `reset` is deliberately excluded even though it cold-starts its successor on a new id, because its own
  `clear_sid` is guarded by `if clear_conversation and session is not None` -- so a reset that keeps the
  conversation writes `session/closed {reset}` while LEAVING the old id mapped, still resumable and its
  log still needed. `discarded` clears the sid unconditionally and would qualify on that test, but no
  path writes that reason into a crew log today, so admitting it would be a rule about a file nothing
  produces. Every other reason -- a shutdown, a crash, an eviction, or a spelling this build does not
  know -- ends the gateway's SERVICE of the session without ending the id's life. An absent, empty or
  non-string reason is read as absent, never matched.

  Because the deciding entry is the newest LIFECYCLE one, a `session/opened` after a `destroyed` puts the
  unit back out of reach. So planting a mapping and resuming causes the log to be KEPT, never deleted:
  every way of making the id reachable again also makes the unit uncollectable.

  This reason is the WHOLE authorization for deleting a unit, and it is read from the crew log rather than
  from anything outside it. Asking `session_map.json` which sessions are still mapped, and keeping those,
  was implemented and then removed: that file is agent-WRITABLE while this tree is bind-masked, and a
  VALID empty map is not a failed read -- it reads as "nothing is revivable" and hands the trusted sweep
  a positive answer that authorizes deleting a fenced unit the writer of that file cannot touch directly.
  Absence of protection must never be authorization, which is why the rule needs positive proof from
  inside the fence. It also subsumes the revival race it replaced: an id whose mapping the gateway
  DELETED cannot be resumed at all, so there is no window between a revival's authorization and its
  first entry.

**Being UNREADABLE is not one of them.** Segment provenance is checked on the read path: a segment
whose header names another unit or another schema version, or whose filename declares a first sequence
its own first entry does not carry, refuses the whole read with `bad_segment`. The sweep never goes
through that path -- it reads the header line and a bounded tail directly -- so a unit the reader
refuses is still collected on age. That is deliberate. Gating removal on readability would make a
damaged crew log IMMORTAL: the one unit nothing can use would be the one unit retention could never
reclaim, and the corruption would be preserved forever by the rule meant to protect history. None of
the three refusals above needs the entries to be readable end to end, so what is dropped is a closed,
aged, unowned unit either way.

The age comes from the close entry's own `time`, and the newest segment's MTIME is a fallback used
only when that field is unusable. The entry wins because it is the writer's own record of when the
session ended and nothing rewrites it, while mtime is metadata a copy, a restore or a backup tool
resets -- a restored tree would read as freshly closed and never expire. The fallback is kept rather
than skipping the unit because a damaged `time` still proves the session ended, and mtime is then the
best available bound on when writing stopped; it can only be at or after the real close, so it errs
toward keeping the file. The LAST close in the file is the one read, because a resumed session
appends to the crew log it already had and only the newest close describes the life that ended.

**Permanently deleting a session removes its crew log; closing a tab does not.** The dashboard's
history-delete funnel calls `remove_unit` with the ACP session id it captured BEFORE the teardown,
and only once that teardown SUCCEEDED. The gate is on success rather than on the attempt because
`destroy_if` refuses when the session was replaced by a successor generation, is busy, or still has a
live slot owner -- in each of those cases the session it names is preserved and may be writing right
now. The write lease is not a substitute for that check: ownership ends between turns by design, so an
idle-but-live session holds nothing for the removal to be refused by. Only an id the delete claim
PROVED is used -- it is dropped on every path that disowns the slot, and a value that is not a
non-empty string reads as absent -- so an unresolvable one removes nothing and the sweep collects that
crew log on age instead. The removal is best-effort: the transcript row is already gone by then, so
raising would ask a person to retry a delete against a row that is absent.

**Every destroy records its teardown, and without that entry neither half collects the unit.**
`session_lifecycle.destroy` writes `session/closed {destroyed}` through the emitter, beside the
`reset` route that already did. It is not bookkeeping. The emitter holds a destroyed session's cached
handle, and the write lease that handle carries, so a removal claiming the lease `sole` answers
`owned`; the sweep is blocked from the other side, because a unit whose newest lifecycle entry is not
a close reads as OPEN whatever its age. One missing entry defeated both paths, which is why the entry
is written where every destroy passes rather than only in the delete funnel. Writing it cannot start a
file either -- the emitter never creates a crew log for a session that has none -- and the entry itself
is what RELEASES the handle, since `on_session_closed` drops it once no turn of that session is
pinned. The delete funnel therefore FLUSHES before it claims the lease, waiting for that release; a
flush that times out is not a failure, because the entry stays owed and the sweep collects the unit
once it lands.

**The unit's own HEADER has to name the slot being deleted.** The id above arrives from
`session_map.json`, which lives inside the agent-visible tree, so a mapping that named another
conversation's session would aim this removal at that conversation's log. `store.unit_header_slot`
is the independent answer: the header is written once at creation inside the FENCED crew log tree and is
never rewritten, so it does not move when a mapping does, and a unit belonging to another slot fails the
check. Slot recycling does not weaken it, because the id is what selects the unit and the slot only has
to prove that unit belonged to the slot being deleted -- a successor in the same slot is selected by its
own id and passes on its own header. The reader answers `None`, and the removal is refused, for every
reason a caller must not proceed on: no directory, no segment, an unreadable header, a header whose own
id does not fold back to its directory, or a header with no slot -- which is the case for a session that
never ran on a dashboard slot, and those are left to the sweep rather than removed on a guess.

That is the opposite of the rule for the WORK ledger, which the same funnel deliberately PRESERVES
(`session-work-ledger.md`), and the difference is mechanical rather than a re-reading of that ruling.
A work ledger is keyed by the SLOT KEY, which is recycled: a successor tab in the same slot
legitimately inherits and resumes that record, and no in-process check can prove one is not about to
appear, so deleting it can destroy a successor's resumable state. The session's log is keyed by the ACP
SESSION ID, which never names a different conversation, so the removal cannot reach a successor at
all; and it holds a kernel-arbitrated lease, so a writer that IS still there refuses the removal
rather than racing it. Neither property is available to the work ledger, which is why one is collected
here and the other is not.

## 9. Scope

The first consumer is the session-log emitter (`docs/system-specs/modules/crew-log-emitter.md`), which writes the ACP turn lifecycle behind the `KIROCREW_CREW_LOG` flag. No crew writer exists yet, so the crew half of the ownership registry has no emitter. That is safe to leave open because the crew half is a registry of the kind's own domains rather than a list of writers: a guest crew writes those domains under its own `src`, and an app needs no entry at all, since its `app:<name>/` prefix is its permission.

Read and write paths ship together deliberately: the guarantees this format makes -- contiguous seq under a lock, torn-tail repair, refusal before any byte is written -- are each a claim about what a reader sees after a writer acted, so neither half demonstrates them alone. `test/test_crew_log_core.py` exercises them against real files rather than against a mock.
