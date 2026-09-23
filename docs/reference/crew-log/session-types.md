# The session's log entry types

**Local page, not a mirror.** Part of the [crew log reference](README.md), which is
marked as a named exception in [the Reference index](../README.md).

Thirty types. Read [envelope.md](envelope.md) first for the fields every entry
carries; this page covers only each type's `data`.

Session entries are written with `src` `gateway` or `acp` and nothing else. They
never carry `thread`, and no session emitter sets `ref`. A repair closer is the one
writer that can put `gateway` on a type whose ordinary emitter uses `acp`, and it
reuses the last real entry's `time` rather than the clock.

Every subsection below follows one template: summary, kind and `src`, when written,
pairing, fields, invariants, example, reader hint, since.

## Summary

The **Emitter** column says whether this build writes the type. Every row below is
`live`. A type this kind owns with no producing site is kept under
[Removed types](#removed-types) instead of being presented as live API.

| Type | One line | Emitter | `src` | Pairing |
|---|---|---|---|---|
| [`session/opened`](#sessionopened) | The crew log was created, or a claim re-attached to it. | live | `gateway` | — |
| [`session/class`](#sessionclass) | The session's class changed after its log was opened. | live | `gateway` | supersedes `session/opened.class` |
| [`session/closed`](#sessionclosed) | The gateway stopped serving this session. | live | `gateway` | — |
| [`turn/started`](#turnstarted) | A turn was authorized and is about to run. | live | `gateway` | opener of `turn/completed` |
| [`turn/refused`](#turnrefused) | A gate refused to run a dispatched turn. | live | `gateway` | terminal on its own |
| [`turn/completed`](#turncompleted) | A turn ended; its outcome and cost. | live | `acp`, `gateway` | closer, written last |
| [`write/dropped`](#writedropped) | Writer losses, accounted for once. | live | `gateway` | — |
| [`message/received`](#messagereceived) | The body of a message accepted into the session. | live | `gateway` | cites `message/chunk` |
| [`message/sent`](#messagesent) | A finished assistant message. | live | `acp` | cites `message/chunk` |
| [`message/chunk`](#messagechunk) | One slice of an oversize body. | live (overflow only) | inherited | cited by its body entry |
| [`message/queued`](#messagequeued) | A message arrived while a turn was running. | live | `gateway` | — |
| [`request/configured`](#requestconfigured) | The request configuration, when it changed. | live | `gateway` | — |
| [`context/composed`](#contextcomposed) | What was put in front of the model, block by block. | live | `gateway` | — |
| [`step/started`](#stepstarted) | Opens one model call inside a turn. | live | `gateway` | opener of `step/completed` |
| [`step/completed`](#stepcompleted) | Closes one model call. | live | `gateway` | closer |
| [`tool/called`](#toolcalled) | A tool call, arguments digested. | live | `acp` | opener of `tool/completed` |
| [`tool/completed`](#toolcompleted) | A tool call's terminal frame. | live | `acp`, `gateway` | closer, by `call_id` |
| [`approval/requested`](#approvalrequested) | A tool call is waiting on a human. | live | `gateway` | opener of `approval/decided` |
| [`approval/decided`](#approvaldecided) | How an approval resolved. | live | `gateway` | closer, by `approval_id` |
| [`model/selected`](#modelselected) | A model swap, and why. | live | `gateway` | — |
| [`compaction/applied`](#compactionapplied) | A compaction, as context-usage percentages. | live | `gateway` | — |
| [`plan/updated`](#planupdated) | The session's task list, as just restated. | live | `acp` | — |
| [`background/completed`](#backgroundcompleted) | A model call made on the session's behalf. | live | `gateway` | — |
| [`subagent/spawned`](#subagentspawned) | A child this session dispatched. | live | `gateway` | opener |
| [`subagent/steered`](#subagentsteered) | A correction sent into a running child. | live | `gateway` | — |
| [`subagent/completed`](#subagentcompleted) | A child finished its work. | live | `gateway` | closer, by `agent_id` |
| [`subagent/failed`](#subagentfailed) | A child did not finish its work. | live | `gateway` | closer, by `agent_id` |
| [`ledger/recorded`](#ledgerrecorded) | One session-ledger update: the fields it set and the event explaining them. | live | `gateway` | — |
| [`object/observed`](#objectobserved) | The state of an object outside the session, as a named producer observed it. | live | `gateway` | — |
| [`radar/recorded`](#radarrecorded) | One Issue Radar crew-ledger update: the work-item fields it set and the event explaining them. | live | `gateway` | — |
| [`work/recorded`](#workrecorded) | One work-board mutation: who acted, on which item, and the fields it set. | live | `gateway` | — |

## Session and turn

### `session/opened`

The crew log was created, or this claim re-attached to a conversation already on
disk.

**Kind and `src`** — `session`; `src` is `gateway`.

**When written** — Once per session, on create or on re-attach. A warm reuse of a
handle the process already holds is silent. Not turn-scoped, so it carries no
`turn`.

**Pairing** — None. It is not an opener: `session/closed` is a teardown marker for
the gateway's own serving, not a closer for this entry. On the resume path this
entry's write is the point the interrupted-turn repair runs.

| Field | Type | Required | Meaning | Enum |
|---|---|---|---|---|
| `agent` | string | required | Agent name, defaulted to `kirocrew` when the caller names none. | |
| `slot` | string | required | Slot key. May be empty. | |
| `model` | string | required | Model the backend confirmed is serving this session. Empty when that id is not known. | |
| `model_requested` | string | when this process observed the allocation and a tier resolved one | Model the gateway selected for the allocation that produced this session, before the provider decides whether to send it. | |
| `cwd` | string | required | Working directory. May be empty. | |
| `owner` | string | required | Owner, defaulted to `default`. | |
| `resumed` | bool | required | `true` when this claim re-attached to an existing crew log. | |
| `class` | object | when the gateway could read the slot's memory mode | What kind of session this log belongs to: `memory` (the slot's memory mode, required inside the object), `app` (the app that owns it, when one does), `channel` (`true` when its conversation is published to a messaging channel), `workspace` (the workspace it belongs to). | |
| `previous` | object | | `{sid}` — the crew log the SAME slot was writing before this one. Present only on a crew log that was just created while the slot already had one, and only when that crew log's own header names this slot. Absent on the slot's first crew log, on every re-attach, when the gateway could not name the predecessor, and when the named crew log's header does not name this slot or cannot be read. | |
| `parent` | object | when `session_create` made this session | The creating session, recorded on the child. | |
| `parent.slot` | string | required inside `parent` | The creating session's slot key. | |
| `parent.sid` | string | optional | The creator's ACP session id frozen at mint time; absent when no live handle was available or the retained id was unusable. | |

**Invariants** — At most one per create and one per re-attach. The session's
*starting* model rides here rather than in a `model/selected` entry, which records
only a later swap. The two model fields are a pair and neither is derived from the
other: `model` is what the backend confirmed is serving, `model_requested` is what
the gateway selected for the allocation that produced the session. It is absent
when no tier resolved one AND when this gateway process did not observe that
allocation, as on a re-attach, so its absence is not by itself a claim that
nothing was selected. Selection is not transmission: a model this account cannot
run is withheld inside the provider, so this field names the choice rather than a
message the backend received. An empty `model` is
not a claim that nothing was configured, and a `model_requested` that differs from
`model` is not by itself a refusal — the backend serves the spelling it resolved.

`class` records facts and never a verdict, because the entry cannot be rewritten
and a verdict would freeze one build's reading of a rule into it. Its `memory`
member is required *inside* the object, so the object is never empty and the
object's own presence is what says the class was recorded at all — a reader can
therefore tell a session with nothing to declare (`{"memory":"persistent"}`) from a
log written before the field existed (no `class` at all). The facts are true when
the log is OPENED: a class a session acquires later, such as a channel link added
mid-conversation, is not in them, so a reader that can also see the live session
applies both and refuses on either. A reader deciding whether one session may read
another's log must treat an absent `class` as a refusal rather than as "nothing
applies"; that is what
[reading-from-an-agent.md](reading-from-an-agent.md) means by a closed target
staying decidable.

`model_requested` is written from #12017 onward. An entry older than that carries
no such field whatever the gateway chose, so even the qualified reading of an
absent field holds only for entries written since. A fold spanning the upgrade must
read an absent field on an older entry as *unknown*, which is the same misreading
#12017 exists to remove.

`previous` never names this same session: a re-attach is the same crew log, and a
self-edge would make a chain walker revisit the crew log it started from.

`parent` is different from `previous`: it records who dispatched this session, not
which crew log the same slot used before. It is absent for a person's own tab, a
fork, and a `spawn_run` subagent. The edge is written on the child because the child
learns its ACP session id only when it first runs.

The chain walker is `crew_log/session_tree.fold_slot_chain`
(`crew-log-projection.md` subsection 6.1). It walks this edge newest crew log
first, bounded, refusing to visit an id twice, and stepping only onto a crew log
whose own header slot matches the slot it started on -- so it enforces the
same-slot rule below rather than trusting it. It reports WHY it stopped, and only
`first` means it reached the slot's first crew log; no shipped route calls it yet.

`previous` always names a crew log of the SAME slot, and that is verified rather
than assumed. The id reaches the emitter from the slot-to-session mapping, read
without pruning and latched by whichever allocation observes it first. One limit
is recorded rather than worked around: an allocation whose replay is still pending
does not publish its fresh id over the mapping, so for that window a mapping read
names the crew log BEFORE the newest one — two successive crew logs then cite one
predecessor and the crew log between them is cited by nobody, which a chain walker
steps over without any sign that a crew log is missing. Closing that needs a
deferral that resumes once the predecessor's own writes settle, and it is tracked
with the rest of the supersede work in #12148. The mapping can also name a crew log
the slot never wrote, since an entry can be stale or recycled by the time a
successor cold-starts, so the emitter reads the named crew log's own header —
written once at create, never rewritten — and records the edge only when that
header names this slot. A candidate that cannot be verified gets no edge, so a
reader following one never lands in a crew log the slot never wrote.

```json
{"type":"session/opened","seq":1,"time":1789000000000,"src":"gateway","data":{"agent":"kirocrew","slot":"dashboard:3","model":"","cwd":"/home/u/proj","owner":"default","resumed":false}}
```

```json
{"type":"session/opened","seq":1,"time":1789000000000,"src":"gateway","data":{"agent":"worker","slot":"dashboard:7","model":"","model_requested":"claude-opus-5","cwd":"/home/u/proj","owner":"default","resumed":false}}
```

```json
{"type":"session/opened","seq":1,"time":1789000000000,"src":"gateway","data":{"agent":"worker","slot":"chat-9-1789000000","model":"","cwd":"/home/u/proj","owner":"default","resumed":false,"parent":{"slot":"chat-4-1788900000","sid":"acp-sess-conductor"},"class":{"memory":"persistent"}}}
```

**Reader hint** — `resumed: true` means entries below this line belong to earlier
runs of the same conversation, so a reader building "this run" starts here rather
than at `seq` 1. Read `model` for what serves the session and `model_requested` for
what was chosen. Whether a request was APPLIED is not recorded here: a reader
that needs it reads the provider's own outcome rather than comparing the two
strings. When `model` is empty the served id, once known, appears on the first
`turn/completed` that reports one.

`resumed` and `previous` answer two different continuities, and a reader needs
both. `resumed` covers one crew log served again; `previous` covers one SLOT whose
ACP session was torn down, so its work continues in a crew log with a different
id. A reader that wants the slot rather than the session folds the newest crew
log, reads `previous` off the `status` projection, folds that crew log, and
repeats. A `null` answer is "no edge to follow", never "there was no earlier crew
log": retention deletes whole segments off the front, and the edge rides on the
creating entry.

**Since** — #10091.

### `session/class`

The session's class changed after its log was opened.

**Kind and `src`** — `session`; `src` is `gateway`.

**When written** — At the start of a turn, when the class observed there differs from
the last one this log stated. The ordinary session never produces one.

**Pairing** — Supersedes the `class` object on `session/opened`, and any earlier
`session/class`.

| Field | Type | Required | Meaning | Enum |
|---|---|---|---|---|
| `memory` | string | required | The slot's memory mode, verbatim. | |
| `app` | string | | The app that owns the session, when one does. | |
| `channel` | boolean | | True when the conversation is published to a messaging channel. | |
| `workspace` | string | | The workspace the session belongs to. | |

**Invariants** — Same four members as `session/opened.class`, from one shared
declaration, so the two cannot describe different shapes. A reader takes the most
restrictive value each of the first three ever held: a log published to a channel for one turn
holds that turn's content for good, so the fold does not let a later entry withdraw
a restriction an earlier one recorded.

`workspace` folds differently because it is an identity rather than a restriction:
there is no more-restrictive workspace to keep, so a reader keeps the FIRST one
stated and treats a later different one as the log spanning two workspaces, which
no single workspace's session may read. A dispatch grant is compared against it
because a recorded lineage outlives a workspace switch.

Observed at the START of a turn, which is what makes sampling at turn boundaries
exact rather than approximate. A channel link exists before the inbound message it
routes, so the turn that carries a third party's words into the log is a turn whose
opening observation already saw the link that carried them. A class acquired
part-way through a turn is recorded on the next one, and the only content inside
that window is the session's own.

Absence means the class never moved — but only on a log whose `session/opened`
carries a `class`. The two landed together, so a class on the opener is what dates a
log to a build that also records transitions; an opener without one says nothing
about either, and a reader deciding whether another session may read the log refuses
on it.

```json
{"type":"session/class","seq":94,"time":1789000070000,"src":"gateway","data":{"memory":"persistent","channel":true}}
```

### `session/closed`

The gateway stopped serving this session, for a stated reason.

**Kind and `src`** — `session`; `src` is `gateway`.

**When written** — At teardown. Carries no `turn`.

**Pairing** — None.

| Field | Type | Required | Meaning | Enum |
|---|---|---|---|---|
| `reason` | string | required | The gateway's own end reason, verbatim. | |

**Invariants** — **Not the end of the file.** Entries from turns already in flight
may land after it.

```json
{"type":"session/closed","seq":210,"time":1789000090000,"src":"gateway","data":{"reason":"reset"}}
```

**Reader hint** — Do not stop folding here, and do not treat a later entry as
corruption. Read to the end of the last segment.

**Since** — #10091.

### `turn/started`

A turn was authorized and is about to run.

**Kind and `src`** — `session`; `src` is `gateway`.

**When written** — Once the turn is authorized, after the permit, shutdown and
stop-before-dispatch gates. A `turn/started` therefore always means the turn ran.

**Pairing** — Opener. Closed by [`turn/completed`](#turncompleted). An open
`turn/started` at the tail is what repair closes.

| Field | Type | Required | Meaning | Enum |
|---|---|---|---|---|
| `turn` | int | required | Message-boundary ordinal identifying the turn. | |
| `actor` | string | required | Who caused the turn. An unrecognized value is folded to `other`. | `user`, `app`, `crew`, `cron`, `autonudge`, `subagent`, `gateway`, `other` |
| `depth` | int | required | Prompt depth. | |
| `message_seq` | int | optional | `seq` of the causing message entry. Omitted when 0 or unknown. | |
| `attempt` | int | optional | Which try at this ordinal. Omitted at 1; present and greater than 1 on a rerun of the same ordinal. | |

**Invariants** — A turn ordinal may appear more than once when a turn is rerun;
`attempt` is what separates the tries.

```json
{"type":"turn/started","seq":12,"time":1789000000200,"src":"gateway","data":{"turn":3,"actor":"user","depth":0}}
```

**Reader hint** — Key a turn on `(turn, attempt)` rather than `turn` alone, taking
a missing `attempt` as 1.

**Since** — #10091.

### `turn/refused`

A turn was dispatched but a gate refused to run it.

**Kind and `src`** — `session`; `src` is `gateway`.

**When written** — At the refusing gate, instead of a `turn/started`.

**Pairing** — Terminal for that ordinal on its own. It is deliberately not an open
opener, so a refusal is never mistaken for a turn that died mid-flight and never
attracts a repair closer.

| Field | Type | Required | Meaning | Enum |
|---|---|---|---|---|
| `turn` | int | required | Turn ordinal. | |
| `actor` | string | required | Who caused the turn. Same folding as `turn/started`. | `user`, `app`, `crew`, `cron`, `autonudge`, `subagent`, `gateway`, `other` |
| `reason` | string | required | Which gate refused. The writer records the caller's value without constraining it. | `not_authorized`, `gateway_closing`, `stopped_before_dispatch`, `replay_superseded_before_dispatch`, `blocked`, `too_large` |
| `depth` | int | required | Prompt depth. | |

**Invariants** — No `turn/completed` follows it for that ordinal.

```json
{"type":"turn/refused","seq":13,"time":1789000000210,"src":"gateway","data":{"turn":4,"actor":"user","reason":"stopped_before_dispatch","depth":0}}
```

**Reader hint** — Count refusals separately from turns. Folding them together
makes a session look busier than it was.

**Since** — #10091.

### `turn/completed`

A turn ended; records its outcome and its cost.

**Kind and `src`** — `session`; `src` is `acp` on the measured close, `gateway` on
the failed close and on a repair closer.

**When written** — From the turn's `finally`, after all output has flushed —
not from the terminal stream event. Three writers reach it: the measured close, the
in-process failed close, and crash repair.

**Pairing** — **Closer, and written last.** Its position marks the turn boundary,
so anything after it belongs to an already-closed turn. Closes
[`turn/started`](#turnstarted). Repair writes it with `stop_reason` `interrupted`
for a turn that was still open.

| Field | Type | Required | Meaning | Enum |
|---|---|---|---|---|
| `turn` | int | required | Turn ordinal. | |
| `depth` | int | on a measured or failed close | Prompt depth. Absent on a crash-repair closer. | |
| `stop_reason` | string | required | How the turn ended. `failed` is the in-process failed close; `interrupted` is written **only by crash repair**; otherwise the provider's terminal reason. | `end_turn`, …, `failed`, `interrupted` (repair only) |
| `duration_ms` | int | on a measured or failed close | Measured turn duration. Absent on a crash-repair closer, which has none to report. | |
| `model` | string | on a measured or failed close | Model the turn served on. Absent on a crash-repair closer. | |
| `provider` | string | on a measured or failed close | Provider. Absent on a crash-repair closer. | |
| `credits` | float | optional | Present on the measured close, absent on a synthesized one. | |
| `tokens` | object | optional | `{input, output, cache_read, cache_write}`, all ints. Present with `credits`, absent on a synthesized close. | |
| `error` | string | optional | Exception class name — never its message — on the failed close. | |

**Invariants** — `credits` and `tokens` travel together. An in-process close
carrying neither is synthesized, and its `duration_ms` is still real. A crash-repair
closer is narrower than either: it carries `turn` and `stop_reason` and nothing else,
so a fold must read every other field with a default rather than by subscript. Cost is
measured per turn and appears only here, never on `message/sent`.

```json
{"type":"turn/completed","seq":40,"time":1789000001500,"src":"acp","data":{"turn":3,"depth":0,"stop_reason":"end_turn","duration_ms":1300,"credits":0.0021,"model":"claude","provider":"anthropic","tokens":{"input":812,"output":143,"cache_read":0,"cache_write":0}}}
```

**Reader hint** — Sum cost over these entries alone. Absent `credits` means
unmeasured, not free, so a total should carry a count of synthesized closes beside
it.

**Since** — #10091.

### `write/dropped`

One durable account of writer losses, before later entries resume.

**Kind and `src`** — `session`; `src` is `gateway`.

**When written** — At the head of the session's next batch after a loss. Further
loss merges into the same pending marker rather than adding entries.

**Pairing** — None.

| Field | Type | Required | Meaning | Enum |
|---|---|---|---|---|
| `dropped_count` | int | required | How many appends were lost. | |
| `dropped_bytes` | int | required | Size hint for the lost jobs. | |

**Invariants** — It records a hole without making one: a lost job never took a
`seq`, so `seq` stays contiguous across the loss. It carries **no reason code**,
because the writer cannot separate a malformed entry from an ownership refusal at
the point it gives up.

```json
{"type":"write/dropped","seq":55,"time":1789000002000,"src":"gateway","data":{"dropped_count":3,"dropped_bytes":2048}}
```

**Reader hint** — This is the one entry that says the record is incomplete. A
reader reporting on a session should surface it rather than fold it away, and
should not try to infer *which* facts are missing.

**Since** — #10091.

## Message, request and step

### `message/received`

The body of a message the gateway accepted into this session.

**Kind and `src`** — `session`; `src` is `gateway`.

**When written** — Before the dispatch gates, so a refused turn still shows what
was said. A turn a gate refuses — including a blocked or oversized `@prompt`
expansion — writes this entry and then a [`turn/refused`](#turnrefused), the same
pair every dispatch gate records.

**Pairing** — None, but on an oversize body it cites its
[`message/chunk`](#messagechunk) entries and is written in the same batch as them.

| Field | Type | Required | Meaning | Enum |
|---|---|---|---|---|
| `turn` | int | required | Turn ordinal. | |
| `role` | string | required | Message role. | |
| `source` | string | required | Surface the message arrived on. May be empty. | |
| `text` | string | conditional | The redacted body, when it fits one line. Replaced by `chunks` when it does not. | |
| `attachments` | [string] | optional | Attachment ids. Present only when there is at least one. These are ids, not `ref`s. | |
| `attachments_omitted` | int | optional | How many ids were dropped because the list would not fit. | |
| `chunks` | [int] | optional | Chunk `seq`s, present instead of `text` on an overflow body. | |
| `chars` | int | optional | Character count of the full body, present with `chunks`. | |

**Invariants** — Exactly one of `text` or `chunks` is present. The body is redacted
at the emitter — exfiltration URLs first, then credentials — before it is measured
or split.

```json
{"type":"message/received","seq":11,"time":1789000000180,"src":"gateway","data":{"turn":3,"role":"user","text":"fix the build","source":"dashboard"}}
```

**Reader hint** — Handle both body forms. Reconstruct an overflow body by reading
the `chunks` seqs in order and concatenating their `delta` values.

**Since** — #10091.

### `message/sent`

A finished assistant message — one model call's worth of text.

**Kind and `src`** — `session`; `src` is `acp`.

**When written** — When a reply is complete. An empty body writes nothing.

**Pairing** — None, but cites its [`message/chunk`](#messagechunk) entries on an
oversize body.

| Field | Type | Required | Meaning | Enum |
|---|---|---|---|---|
| `turn` | int | required | Turn ordinal. | |
| `step` | int | optional | Model call ordinal. Omitted when 0 or unknown. | |
| `text` | string | conditional | The redacted body, when it fits one line. | |
| `interrupted` | bool | optional | Written only as `true`, when a steer cut this reply. | `true` |
| `chunks` | [int] | optional | Chunk `seq`s, present instead of `text` on overflow. | |
| `chars` | int | optional | Character count of the full body, present with `chunks`. | |

**Invariants** — Exactly one of `text` or `chunks`. `interrupted` is never written
`false`, so absence means "not interrupted". This entry carries no usage: cost
rides on [`turn/completed`](#turncompleted).

```json
{"type":"message/sent","seq":39,"time":1789000001400,"src":"acp","data":{"turn":3,"step":2,"text":"Done."}}
```

**Reader hint** — Several of these per turn is normal — one per model call. Do not
treat the first as the turn's answer.

**Since** — #10091.

### `message/chunk`

One slice of an oversize body.

**Kind and `src`** — `session`; `src` is inherited from the body entry that cites
it — `acp` under a `message/sent`, `gateway` under a `message/received`.

**When written** — Only by the overflow split, never on its own.

**Pairing** — Cited by the body entry that names its `seq` in `chunks`. All the
chunks and the citing entry are written in one batch, so a reader either sees the
whole group or none of it.

| Field | Type | Required | Meaning | Enum |
|---|---|---|---|---|
| `turn` | int | required | Turn ordinal. | |
| `step` | int | optional | Model call ordinal, on assistant bodies. | |
| `delta` | string | required | One redacted slice of the body. | |

**Invariants** — Always `ignorable: true`. A **trailing** chunk run whose citing
entry never landed is truncated by repair, so a chunk that survives always has a
citing entry.

```json
{"type":"message/chunk","seq":41,"time":1789000001410,"src":"acp","ignorable":true,"data":{"turn":3,"step":2,"delta":"the first slice of the body"}}
```

**Reader hint** — Never read these directly. Start from a body entry's `chunks`
list; a reader that does not understand the type may skip them, which is what
`ignorable` promises.

**Since** — #10091.

### `message/queued`

A message arrived while a turn was already running.

**Kind and `src`** — `session`; `src` is `gateway`.

**When written** — On arrival, when a turn is in flight.

**Pairing** — None.

| Field | Type | Required | Meaning | Enum |
|---|---|---|---|---|
| `source` | string | required | Surface it arrived on. | |
| `bytes` | int | required | Size of the queued message. | |
| `queued_seq` | string | required | The queue entry's own id. A string, not a crew log `seq`. | |

**Invariants** — Carries **no `turn`**: it belongs to no turn yet. The body is not
recorded here; it lands in [`message/received`](#messagereceived) when the queue
drains.

```json
{"type":"message/queued","seq":30,"time":1789000000900,"src":"gateway","data":{"source":"slack","bytes":214,"queued_seq":"q-8"}}
```

**Reader hint** — `queued_seq` names a queue slot and must not be resolved as a
crew log `seq` or fed to a `ref`.

**Since** — #10091.

### `request/configured`

The request configuration, recorded when it changed.

**Kind and `src`** — `session`; `src` is `gateway`.

**When written** — Change-only per session. An unchanged configuration is silent,
and the fingerprint is remembered only after the line lands.

**Pairing** — None.

| Field | Type | Required | Meaning | Enum |
|---|---|---|---|---|
| `turn` | int | required | Turn ordinal. | |
| `model` | string | required | Model. | |
| `provider` | string | required | Provider. | |
| `context_window` | int | required | Context window size. | |
| `system` | string | optional | sha256 digest of the system prompt, when one is supplied. | |
| `system_bytes` | int | optional | Byte length of the system prompt, present with `system`. | |

**Invariants** — The system prompt is digested, never recorded. No resolved tool
list is written: with tool search on, the gateway does not receive one.

```json
{"type":"request/configured","seq":10,"time":1789000000150,"src":"gateway","data":{"turn":3,"model":"claude","provider":"anthropic","context_window":200000}}
```

**Reader hint** — The absence of this entry on a turn means the configuration
matches the last one recorded, not that it is unknown. Carry the value forward.

**Since** — #10091.

### `context/composed`

What the gateway put in front of the model, block by block.

**Kind and `src`** — `session`; `src` is `gateway`.

**When written** — Per model call that composes a prompt. Nothing is written when
there are no blocks to tally.

**Pairing** — None.

| Field | Type | Required | Meaning | Enum |
|---|---|---|---|---|
| `turn` | int | required | Turn ordinal. | |
| `sources` | [object] | required | Per-block `{kind, chars, tokens}`, ordered by descending `chars`. | |
| `chars` | int | required | Total characters. | |
| `tokens` | int | required | Estimated tokens. | |
| `tokens_estimated` | bool | required | Always `true`. | `true` |
| `step` | int | optional | Model call ordinal. Omitted when 0. A turn-opening composition is written before the first `step/started`, so it is step-less; join it to the turn's FIRST model call. | |

**Invariants** — `tokens` is an estimate derived from `chars`, which is why
`tokens_estimated` is written on every entry rather than only when it is true.
Blocks with no classification are folded into a single `other` source, so `sources`
does not enumerate every injected block by name. A step-less `context/composed`
belongs to its turn's first model call: the context is composed once, in front of
the call that opens as step 1, and the entry is written before that opener, so it
cannot carry the ordinal without dropping below the `message/received` it is derived
from.

```json
{"type":"context/composed","seq":9,"time":1789000000140,"src":"gateway","data":{"turn":3,"sources":[{"kind":"system","chars":4000,"tokens":1000},{"kind":"other","chars":1200,"tokens":300}],"chars":5200,"tokens":1300,"tokens_estimated":true}}
```

**Reader hint** — Do not report these token numbers as billed usage. The billed
figures are on [`turn/completed`](#turncompleted). An entry with no `step` is the
turn's opening composition; attribute it to the turn's first model call.

**Since** — #10091.

### `step/started`

Opens one model call inside a turn.

**Kind and `src`** — `session`; `src` is `gateway`.

**When written** — Right after `turn/started` once the turn is authorized, and
again at each transition from a tool group back to text.

**Pairing** — Opener. Closed by [`step/completed`](#stepcompleted).

| Field | Type | Required | Meaning | Enum |
|---|---|---|---|---|
| `turn` | int | required | Turn ordinal. | |
| `step` | int | required | Model call ordinal, from 1. | |

**Invariants** — Steps are numbered within a turn, so `(turn, step)` identifies a
model call. The boundary is DERIVED from a tool group followed by fresh text, the
only per-call transition the stream exposes, so a step MAY cover consecutive
tool-only model calls: a turn that calls tools, is called again with their results
and calls more tools, speaking only at the end, shows one such transition and folds
those calls into one step.

```json
{"type":"step/started","seq":14,"time":1789000000220,"src":"gateway","data":{"turn":3,"step":1}}
```

**Reader hint** — Step count per turn is a LOWER BOUND on the turn's model calls,
not an exact count: consecutive tool-only calls may share one step. Use `call_index`
on the tool entries to order every tool call regardless of how the steps fell.

**Since** — #10091.

### `step/completed`

Closes one model call and records how long it took.

**Kind and `src`** — `session`; `src` is `gateway`.

**When written** — At the next transition, and beside the turn's completion event.
Nothing is written when the step ordinal is 0.

**Pairing** — Closer for [`step/started`](#stepstarted). Repair writes no step
closer, so a step may be left open by a crash.

| Field | Type | Required | Meaning | Enum |
|---|---|---|---|---|
| `turn` | int | required | Turn ordinal. | |
| `step` | int | required | Model call ordinal. | |
| `ms` | int | required | Duration, never negative. | |

**Invariants** — `ms` measures the model call, not the turn.

```json
{"type":"step/completed","seq":38,"time":1789000001390,"src":"gateway","data":{"turn":3,"step":1,"ms":900}}
```

**Reader hint** — An open `step/started` with a `turn/completed` after it means the
turn ended mid-step. That is expected, not damage.

**Since** — #10091.

## Tool and approval

### `tool/called`

A tool call, identified by id. Arguments are digested, never recorded.

**Kind and `src`** — `session`; `src` is `acp`.

**When written** — On the call frame.

**Pairing** — Opener. Closed by [`tool/completed`](#toolcompleted) matched on
`call_id`.

| Field | Type | Required | Meaning | Enum |
|---|---|---|---|---|
| `turn` | int | required | Turn ordinal. | |
| `call_id` | string | required | Tool call id. Empty when the frame carried none. | |
| `name` | string | required | Trusted tool name. Empty when the backend supplied none. | |
| `server` | string | required | MCP server name. Empty when there is none. | |
| `kind` | string | required | Tool kind. | |
| `call_index` | int | optional | Position among the turn's tool calls. Omitted when 0. | |
| `step` | int | optional | Model call that issued it. Omitted when 0. | |
| `args_hash` | string | optional | sha256 of the serialized arguments, when there are any. | |
| `args_bytes` | int | optional | Byte length of the serialized arguments, present with `args_hash`. | |

**Invariants** — Argument *content* never reaches the crew log. `args_hash` lets two
calls be compared for equality without recording what was passed.

```json
{"type":"tool/called","seq":16,"time":1789000000300,"src":"acp","data":{"turn":3,"call_id":"c-01","name":"read","server":"","kind":"fs","call_index":1,"step":1,"args_hash":"9f2b7c41","args_bytes":42}}
```

**Reader hint** — `call_id` may be empty, so it is not a safe dictionary key on its
own. Fall back to `(turn, call_index)`.

**Since** — #10091.

### `tool/completed`

A tool call's terminal frame. Results are digested, never recorded.

**Kind and `src`** — `session`; `src` is `acp` on both the ordinary close and the
turn-end sweep, and `gateway` only on a repair closer.

**When written** — Three writers: the ordinary terminal frame; the sweep that
closes still-open calls at a tool-group boundary or at turn end; crash repair.

**Pairing** — Closer for [`tool/called`](#toolcalled), matched on `call_id`.

| Field | Type | Required | Meaning | Enum |
|---|---|---|---|---|
| `turn` | int | required | Turn ordinal. | |
| `call_id` | string | required | The same id as the call. | |
| `name` | string | required | Filled from the remembered call frame. | |
| `server` | string | required | Filled from the remembered call frame. | |
| `status` | string | required | Outcome, as an **open set**: the terminal frame passes the backend's own word through unmapped, so a value outside the list below is possible and must not be treated as invalid. `refused` is decided by this process and wins over the frame's word. The sweep writes `completed` at a tool-group boundary and `unknown` at turn end; repair writes `unknown` for a call it cannot match. | `completed`, `failed`, `cancelled`, `canceled`, `refused`, `unknown` |
| `call_index` | int | optional | Present when known. | |
| `step` | int | optional | Present when known. | |
| `elapsed_ms` | int | optional | Present when the call frame was still in memory. | |
| `is_error` | bool | optional | Tri-state: omitted when the caller made no assertion either way. | |
| `result_hash` | string | optional | sha256 of the redacted result, when there is one. | |
| `result_bytes` | int | optional | Byte length. `0` on an output-less close, absent when there was nothing to digest. | |

**Invariants** — `status: "unknown"` means the writer could not observe the
outcome, not that the tool failed. Both `cancelled` and `canceled` occur, because the
word is the backend's and is not normalized on the way in. A sweep close carries
`result_bytes: 0` and no `result_hash`. Exactly one `tool/completed` is written per
`call_id`: the first terminal frame settles the call, and a later terminal frame for
the same id — the two update parsers can each emit one — writes nothing. A terminal
frame for a call whose `tool/called` was never recorded still gets its closer, so an
unmatched completion is a real close rather than a dropped one.

```json
{"type":"tool/completed","seq":18,"time":1789000000400,"src":"acp","data":{"turn":3,"call_id":"c-01","name":"read","server":"","status":"completed","call_index":1,"step":1,"elapsed_ms":90,"result_hash":"1a3c9e02","result_bytes":512}}
```

**Reader hint** — Do not count `unknown` as a failure. Distinguish it from
`is_error: true`, which is a real reported error.

**Since** — #10091.

### `approval/requested`

A tool call is waiting on a human.

**Kind and `src`** — `session`; `src` is `gateway`.

**When written** — One statement before the `try` whose `finally` records the
decision, so a request can never be lost while its decision is written.

**Pairing** — Opener. Closed by [`approval/decided`](#approvaldecided) matched on
`approval_id`.

| Field | Type | Required | Meaning | Enum |
|---|---|---|---|---|
| `turn` | int | required | Turn ordinal. | |
| `approval_id` | string | required | Approval request id. | |
| `tool` | string | optional | Tool name. Absent when the frame named none. | |
| `reason` | string | optional | The redacted, clipped title shown to the human. Absent when there is none. | |

**Invariants** — `reason` is what a person saw, not the tool's arguments.

```json
{"type":"approval/requested","seq":24,"time":1789000000600,"src":"gateway","data":{"turn":3,"approval_id":"a-1","tool":"shell","reason":"remove the build directory"}}
```

**Reader hint** — An unmatched request means the process died while a human was
still deciding.

**Since** — type #10091; written by #11185.

### `approval/decided`

How an approval resolved.

**Kind and `src`** — `session`; `src` is `gateway`.

**When written** — From the `finally` that every exit path converges on: a human
answer, a timeout, a no-budget decline, a delivery failure, a cancelled wait.

**Pairing** — Closer for [`approval/requested`](#approvalrequested). Repair closes
an unmatched request with `decision` `unknown`.

| Field | Type | Required | Meaning | Enum |
|---|---|---|---|---|
| `turn` | int | required | Turn ordinal. | |
| `approval_id` | string | required | The same id as the request. | |
| `decision` | string | required | The decision. `unknown` is written **only by crash repair**. | `unknown` (repair only) |
| `by` | string | optional | Written only for a host-made decision. Omitted for a person's own answer. | `host` |
| `cause` | string | optional | The host's reason code for an automatic decline. | |

**Invariants** — Absence of `by` is the signal that a human answered. A reader must
not read it as an unattributed decision.

```json
{"type":"approval/decided","seq":25,"time":1789000000650,"src":"gateway","data":{"turn":3,"approval_id":"a-1","decision":"approved"}}
```

**Reader hint** — To count what a person actually approved, filter to entries with
no `by`.

**Since** — type #10091; written by #11185.

## Model, compaction and plan

### `model/selected`

The model a session will serve, and why it was chosen.

**Kind and `src`** — `session`; `src` is `gateway`.

**When written** — On a swap, such as a fallback. The session's starting model is
not written here; it rides on [`session/opened`](#sessionopened).

**Pairing** — None.

| Field | Type | Required | Meaning | Enum |
|---|---|---|---|---|
| `model` | string | required | Model id. | |
| `source` | string | required | Why this model was chosen. | |
| `turn` | int | optional | The turn the pick was made for. Omitted when 0 or outside a turn. | |

**Invariants** — An entry here means the served model differs from the one
`session/opened` recorded.

```json
{"type":"model/selected","seq":26,"time":1789000000700,"src":"gateway","data":{"model":"claude-fallback","source":"fallback","turn":3}}
```

**Reader hint** — To know which model served turn N, take the latest
`model/selected` at or before it, falling back to `session/opened`.

**Since** — #10091.

### `compaction/applied`

A compaction, recorded as context-usage percentages.

**Kind and `src`** — `session`; `src` is `gateway`.

**When written** — When the compaction verdict settles, which may be later than the
turn it measures.

**Pairing** — None.

| Field | Type | Required | Meaning | Enum |
|---|---|---|---|---|
| `pct_before` | float | required | Context usage percentage before, rounded to 4 decimal places. | |
| `pct_after` | float | required | Context usage percentage after. | |
| `freed_pct` | float | required | `pct_before` minus `pct_after`. Negative when a deferred reading takes in a later turn's growth. | |

**Invariants** — Carries **no `turn`**, because its verdict can settle turns after
the compaction it describes. No raw token counts: the boundary measures only
percentages.

```json
{"type":"compaction/applied","seq":50,"time":1789000001800,"src":"gateway","data":{"pct_before":82.0,"pct_after":41.0,"freed_pct":41.0}}
```

**Reader hint** — A negative `freed_pct` is a real reading, not a bug. Do not clamp
it to zero.

**Since** — #10091.

### `plan/updated`

The session's own task list, as the agent just restated it.

**Kind and `src`** — `session`; `src` is `acp`.

**When written** — Each time the agent restates its list. A `null` list writes
nothing; an empty list is a cleared plan and is written.

**Pairing** — None.

| Field | Type | Required | Meaning | Enum |
|---|---|---|---|---|
| `turn` | int | required | Turn ordinal. | |
| `items` | [object] | required | Each `{id, text, state}`. | `state`: `done`, `open` |
| `total` | int | optional | The real count, written only when the list was clipped by count or by bytes. | |

**Invariants** — A whole list, not a delta. Always `ignorable: true`. `total`
greater than `len(items)` means what is written is a prefix.

```json
{"type":"plan/updated","seq":28,"time":1789000000800,"src":"acp","ignorable":true,"data":{"turn":3,"items":[{"id":"1","text":"read code","state":"done"},{"id":"2","text":"write fix","state":"open"}]}}
```

**Reader hint** — Diff consecutive entries to see progress. Treat an empty `items`
as "plan cleared", not as "no data".

**Since** — type #10091; written by #11185.

## Background work and children

### `background/completed`

A model call the gateway made on this session's behalf.

**Kind and `src`** — `session`; `src` is `gateway`.

**When written** — When such a call finishes. It runs after a turn ends, on a
separate session, so it carries no `turn`.

**Pairing** — None.

| Field | Type | Required | Meaning | Enum |
|---|---|---|---|---|
| `kind` | string | required | Which background call this was. The writer records the caller's value without constraining it; the calling sites emit the values listed here. | `title`, `summary`, `memory_consolidation` |
| `model` | string | optional | Served model. | |
| `provider` | string | optional | Provider. | |
| `credits` | float | optional | Written only when the call was billed, so a zero cost is absent. | |
| `tokens` | object | optional | Only the non-zero billed dimensions of `{input, output, cache_read, cache_write}`. | |
| `ms` | int | optional | Wall clock. Omitted at 0. | |

**Invariants** — Carries **no `turn`**. `tokens` is sparse by construction: a
missing dimension means zero, not unknown.

```json
{"type":"background/completed","seq":60,"time":1789000002100,"src":"gateway","data":{"kind":"title","model":"claude-haiku","provider":"anthropic","credits":0.0001,"tokens":{"input":40,"output":8},"ms":300}}
```

**Reader hint** — Add this cost to a session's total separately from
`turn/completed`; it is real spend that belongs to no turn. Treat `kind` as an open
set and keep an "other" bucket.

**Since** — type #10091; written by #11185.

### `subagent/spawned`

A child this session dispatched.

**Kind and `src`** — `session`; `src` is `gateway`.

**When written** — When the run actually starts, not when it is queued.

**Pairing** — Opener. Closed by [`subagent/completed`](#subagentcompleted) or
[`subagent/failed`](#subagentfailed), matched on `agent_id`.

| Field | Type | Required | Meaning | Enum |
|---|---|---|---|---|
| `agent_id` | string | required | The child's id. | |
| `turn` | int | optional | The asking turn. Absent when no turn asked — a slash command, a cron, a hook. | |
| `agent` | string | optional | Agent name. | |
| `model` | string | optional | The child's model. | |
| `scope` | object | optional | Context-scope flags `{memory, lessons, project}`, all bools. | |

**Invariants** — Carries **no `ref`**. No subagent path opens a child crew log, so a
citation would name a file that does not exist.

```json
{"type":"subagent/spawned","seq":32,"time":1789000000850,"src":"gateway","data":{"turn":3,"agent_id":"sub-1","agent":"kirocrew","model":"claude","scope":{"memory":false,"lessons":true,"project":true}}}
```

**Reader hint** — A missing `turn` is normal and does not mean the entry is
damaged. Group children by `agent_id`, not by turn.

**Since** — type #10091; written by #11185.

### `subagent/steered`

A correction sent into a running child.

**Kind and `src`** — `session`; `src` is `gateway`.

**When written** — When the steer is sent.

**Pairing** — None. It is written into the parent's crew log, because the child has
none of its own.

| Field | Type | Required | Meaning | Enum |
|---|---|---|---|---|
| `agent_id` | string | required | The child's id. | |
| `mode` | string | optional | How the steer was delivered. | `interrupt`, `follow_up` |

**Invariants** — The steer text is not recorded here.

```json
{"type":"subagent/steered","seq":33,"time":1789000000860,"src":"gateway","data":{"agent_id":"sub-1","mode":"follow_up"}}
```

**Reader hint** — Several of these may sit between one spawn and its close.

**Since** — type #10091; written by #11185.

### `subagent/completed`

A child finished its work.

**Kind and `src`** — `session`; `src` is `gateway`.

**When written** — On the success outcome only.

**Pairing** — Closer for [`subagent/spawned`](#subagentspawned).

| Field | Type | Required | Meaning | Enum |
|---|---|---|---|---|
| `agent_id` | string | required | The child's id. | |
| `ms` | int | optional | Elapsed time. Omitted at 0. | |

**Invariants** — Carries no `tokens` and no `credits`: the subagent runtime
measures neither, so a child's cost is not recoverable from the parent's log.

```json
{"type":"subagent/completed","seq":70,"time":1789000002500,"src":"gateway","data":{"agent_id":"sub-1","ms":40000}}
```

**Reader hint** — Do not attribute child cost from this entry. There is none to
attribute.

**Since** — type #10091; written by #11185.

### `subagent/failed`

A child did not finish its work.

**Kind and `src`** — `session`; `src` is `gateway`.

**When written** — On either non-success outcome.

**Pairing** — Also a closer for [`subagent/spawned`](#subagentspawned). Repair
closes an unmatched spawn with `outcome` `unknown`, and only for a child a
liveness predicate reports finished — with no predicate the spawn is left open
rather than closed on a guess.

| Field | Type | Required | Meaning | Enum |
|---|---|---|---|---|
| `agent_id` | string | required | The child's id. | |
| `outcome` | string | optional | Which non-success outcome. Defaults to `failed`, so it is absent only when a caller passes an empty value. `unknown` is written **only by crash repair**. | `failed`, `stopped`, `unknown` (repair only) |
| `reason` | string | optional | Redacted, clipped failure reason. | |
| `ms` | int | optional | Elapsed time. Omitted at 0. | |

**Invariants** — This one type covers both non-success outcomes; `outcome` is what
separates them.

```json
{"type":"subagent/failed","seq":71,"time":1789000002510,"src":"gateway","data":{"agent_id":"sub-2","reason":"provider error","outcome":"failed","ms":12000}}
```

**Reader hint** — A spawn with no close at all means the parent died while the
child was running and no liveness predicate was available.

**Since** — type #10091; written by #11185.

## The session ledger

### `ledger/recorded`

One session-ledger update: the fields it set, and the event explaining them.

**Kind and `src`** — `session`; `src` is `gateway`.

**When written** — One entry per `session_ledger.record` call. Every reader folds
these entries back into the record, so the ledger is a projection of the log rather
than a stored document.

**Pairing** — None.

| Field | Type | Required | Meaning | Enum |
|---|---|---|---|---|
| `slot` | string | required | The ledger's key — the slot this update belongs to. Carried on the entry as well as in the header so a reader of one entry can say which slot it belongs to; selecting a slot's units is done from their headers. | |
| `goal` | string | optional | The workstream's objective, when this call set one. | |
| `phase` | string | optional | The new phase. Never written without `event` and `event_kind`, which is what makes the phase-requires-a-reason rule a property of ONE entry. | |
| `next` | string | optional | The resumable intent — the concrete next step. | |
| `tried` | object | optional | One rejected approach, appended to the fold's list. | |
| `tried.approach` | string | required | What was tried. | |
| `tried.rejected_because` | string | optional | Why it was rejected. | |
| `artifacts` | object | optional | String-to-string pointers merged into the fold's map. The members are the caller's own keys — worktree, branch, pr — so they are deliberately not declared and are checked for shape by the fold. | |
| `event` | string | optional | One-line progress note appended to the event tail. | |
| `event_kind` | string | optional | Which kind of step this records. Closed: the writer coerces an unrecognized kind to `note` before it builds the entry. | `blocked`, `decision`, `note`, `phase`, `progress`, `tried`, `unblocked` |

**Invariants** — One entry per call, carrying only the fields that call set — an
omitted field means "unchanged", which is what lets a partial update be one line. A
phase change carries its event in the SAME entry, so no reader can observe a phase
that moved without its logged reason. The ledger therefore DEPENDS on this log: a
gateway started without `KIROCREW_CREW_LOG=1` records none, and the tool refuses
rather than keeping a document of its own.

```json
{"type":"ledger/recorded","seq":80,"time":1789000002600,"src":"gateway","data":{"slot":"dashboard:3","goal":"land the ledger fold","phase":"implementation","next":"regenerate the reference tables","tried":{"approach":"stored document","rejected_because":"cannot survive compaction"},"artifacts":{"worktree":"/w/proj","branch":"feat/x","pr":"123"},"event":"folded the ledger over the crew log","event_kind":"phase"}}
```

**Reader hint** — Fold the slot's entries oldest first across every unit the slot
ran under; a later entry's set fields overwrite an earlier one's, and an omitted
field leaves the folded value unchanged.

**Since** — #11185.

## Observed objects

### `object/observed`

The state of an object outside the session — a pull request the session is watching —
as one named producer observed it.

**Kind and `src`** — `session`; `src` is `gateway`.

**When written** — Once per CHANGE of the producer's fingerprint for one subject, never
once per poll. The structured monitor's probe writes it into the log of the session the
monitor was armed from, right after the monitor's persisted observation moved to the new
fingerprint; a poll that saw the same fingerprint, a failed read, an observation the
monitor declined, and a slot with no live session each write nothing. The record is
independent of whether anyone was woken: a subject that moved from one pending state to
another is recorded even though the engine delivered nothing for it.

**Pairing** — None.

| Field | Type | Required | Meaning | Enum |
|---|---|---|---|---|
| `producer` | string | required | Which mechanism made the observation. Closed: the emitter refuses a value outside the vocabulary instead of coercing it, so a reader can tell a measured record from a sentence an agent typed. `probe` is the structured monitor's provider probe. | `probe` |
| `kind` | string | required | The monitored kind of the subject, as the monitoring registry names it — `github_pull_request`, `gitlab_merge_request`, and so on. Passed through from the armed monitor, which validated it at arm time. | |
| `target` | string | required | The subject's full URL, exactly as the monitor was armed on it. | |
| `fingerprint` | string | required | The probe's own dedupe digest of the facts it acts on. An entry is written only when this differs from the previous observation's, so consecutive entries for one subject are consecutive DISTINCT states. | |
| `facts` | object | required | The canonical facts snapshot the probe computed, verbatim — the object the wake envelope is rendered from, including its own `kind` and `target`. The members are the kind's canonical vocabulary and are deliberately not declared: a fact the probe could not establish is absent or carries the kind's own unknown marker, never a default the registry invented. | |
| `facts_omitted` | array[string] | optional | Members removed from `facts` so the entry fits the line ceiling, largest first. Absent when nothing was removed, which is the ordinary case. | |
| `observed_at` | float | required | When the producer observed the subject, seconds since the epoch. Distinct from the envelope's `time`, which is when the append landed. | |

**Invariants** — `producer` is a closed vocabulary, and the closure is enforced twice:
the emitter raises on a value outside it and the registry refuses the entry on append.
A typed record carrying its producer is what a reader can trust about an object outside
the session; the agent's own report about that object is a `message/sent` entry and is
evidence of nothing but the report. `facts` is never defaulted: a snapshot too large for
one line is recorded short by a NAMED member rather than dropped or trimmed silently,
so a reader cannot mistake "did not fit" for "unchanged".

```json
{"type":"object/observed","seq":81,"time":1789000002700,"src":"gateway","data":{"producer":"probe","kind":"github_pull_request","target":"https://github.com/acme/widgets/pull/7","fingerprint":"9f2b9f2b9f2b9f2b9f2b9f2b9f2b9f2b9f2b9f2b9f2b9f2b9f2b9f2b9f2b9f2b","facts":{"kind":"github_pull_request","target":"https://github.com/acme/widgets/pull/7","state":"open","draft":false,"head_revision":"abc123","mergeability":"mergeable","review_decision":"approved","blocking_review":"none","unresolved_review_threads":0,"review_threads_complete":true,"checks":{"failed":[],"passed":["ci"],"pending":[],"unknown":[]},"checks_complete":true},"observed_at":1789000002.5}}
```

**Reader hint** — Group by `target` and take the newest entry for the subject's current
state; an entry's `facts` is complete in itself, so nothing needs to be folded across
entries. Read `state`, `mergeability`, `review_decision` and the `checks` buckets off
`facts` for a review subject, and treat a member that is absent or listed in
`facts_omitted` as unknown, never as its default.

**Since** — the producer half of #12397.

## The Issue Radar crew ledger

### `radar/recorded`

One Issue Radar crew-ledger update: the work-item fields it set, and the event
explaining them.

**Kind and `src`** — `session`; `src` is `gateway`.

**When written** — One entry per `issue_radar_crew_record` call, appended to the
crew log of the session the crew runs on. A crew's work items, its progress lines
and its passes are the `radar` fold of these entries over every unit the crew's
slot ran under, so the ledger is a projection of the log rather than a stored
document. The repository's shared skip index is the union of that fold across
every crew of the repository.

**Pairing** — None.

| Field | Type | Required | Meaning | Enum |
|---|---|---|---|---|
| `crew_id` | string | required | The crew this update belongs to. | |
| `owner` | string | required | Repository owner the crew works in. | |
| `repo` | string | required | Repository name the crew works in. | |
| `number` | int | optional | The issue this update is about. ABSENT on a crew-level step (a queue sweep that took nothing), which is the only kind of entry that patches no work item. | |
| `phase` | string | optional | The item's new phase. Never written without `event` and `event_kind`, which is what makes the phase-requires-a-reason rule a property of ONE entry. Closed: the writer refuses an unknown phase before anything is appended. | `selected`, `claimed`, `investigating`, `implementing`, `awaiting-ci`, `addressing-review`, `awaiting-merge`, `awaiting-reply`, `resolved`, `skipped`, `yielded`, `handed-back`, `preempted` |
| `outcome` | string | optional | Terminal outcome; an empty string clears it. | |
| `decision` | string | optional | What the crew decided to do. | |
| `why` | string | optional | On what grounds. | |
| `next` | string | optional | The resumable intent — the concrete next step. | |
| `tried` | object | optional | One rejected approach, appended to the item's list. | |
| `tried.approach` | string | required | What was tried. | |
| `tried.rejected_because` | string | optional | Why it was rejected. | |
| `worktree` | string | optional | Local only; never echoed into a comment. | |
| `branch` | string | optional | Local only. | |
| `base_sha` | string | optional | Local only. | |
| `pr_number` | int | optional | The pull request this item opened. | |
| `ci_state` | object | optional | CI reading merged into the item's `ci_state` map, key by key. Members are `state`, `passed`, `total`, `round`, `inherited_reds`; the fold keeps no other key and re-bounds each to the record tool's own type and ceiling (a 32-character `state`, counters as ints within the tool's ranges). | |
| `claim_comment_id` | int | optional | Which forge comment carries the claim. | |
| `labels_applied` | array of string | optional | Labels this crew put on the issue, replaced whole; the fold keeps at most 20. | |
| `clear` | array of string | optional | Work-item fields this update EMPTIES, by name. How an explicit null in a record call is carried: a typed field cannot hold one, so the writer names the cleared fields and the fold empties them before applying the fields the same update sets. | `decision`, `why`, `next`, `worktree`, `branch`, `base_sha`, `pr_number`, `claim_comment_id`, `ci_state`, `labels_applied`, `outcome` (open) |
| `skip` | object | optional | Present when this update records a PASS on the issue. The repository's shared skip index is a fold of these across every crew of the repository. | |
| `skip.reason` | string | required | Why the issue was passed over. | |
| `skip.scope` | string | required | Closed: the writer coerces an unknown scope to `other`. | `architecture`, `new-feature`, `needs-design`, `needs-decision`, `needs-investigation`, `duplicate`, `already-fixed`, `not-reproducible`, `wrong-root-cause`, `breaking-change`, `gate-config`, `other` |
| `skip.crew_id` | string | optional | The crew that decided the pass, when it is not the entry's own — only a carried entry sets it. | |
| `skip.decided_at` | string | optional | When the pass was decided, when not this entry's time — carry only. | |
| `skip.deferred` | boolean | optional | True when another crew's decision on this number already stood in the shared index as this pass was recorded. A deferred pass never stands over the decision it saw, whatever the clocks say: the writer's own observation is the first-writer token, not a timestamp. | |
| `carried` | bool | optional | True on an entry that carries a pre-projection on-disk record forward, once, so a crew upgraded mid-work keeps its items and the repository keeps its passes. | |
| `claimed_at` | string | optional | The carried record's own stamp; the fold stamps every other entry itself. | |
| `last_progress_at` | string | optional | Carry only, as `claimed_at`. | |
| `finished_at` | string | optional | Carry only, as `claimed_at`. | |
| `event` | string | required | The public progress line. | |
| `event_kind` | string | required | Which kind of step this records. `sweep` is the one crew-level kind and the only one an entry without `number` may carry. Closed: the writer refuses an unknown kind before anything is appended. | `claim`, `investigate`, `reply`, `implement`, `ci`, `review`, `conflict`, `merge`, `handback`, `skip`, `yield`, `sweep` |

**Invariants** — One entry per call, carrying only the fields that call set — an
omitted field means "unchanged", which is what lets a partial patch be one line. A
phase change carries its event in the SAME entry, and a pass carries its skip row in
the same entry as the phase that records it, so no reader can observe a phase that
moved without its reason or an issue skipped without its index entry. Stamps
(`claimed_at`, `last_progress_at`, `finished_at`) come off the entry's own `time`
except on a carried entry, which re-states a record that already had them. The crew
ledger therefore DEPENDS on this log: a crew whose session has no crew log cannot
record, and the tool refuses rather than keeping a document of its own.

```json
{"type":"radar/recorded","seq":81,"time":1789000002700,"src":"gateway","data":{"crew_id":"c_0a1b2c3d","owner":"kirodotdev","repo":"KiroCrew","number":2251,"phase":"implementing","next":"add the Windows branch to _safe_chmod","tried":{"approach":"hasattr guard","rejected_because":"loses the ACL"},"branch":"fix/safe-chmod-2251","pr_number":2271,"ci_state":{"state":"running","round":3},"event":"entered implementing: the test already fails","event_kind":"implement"}}
```

**Reader hint** — Fold a crew's entries oldest first across every unit its slot ran
under, the live unit last; a later entry's set fields overwrite an earlier one's, an
omitted field leaves the folded value unchanged, a name in `clear` empties that
field before the same entry's set fields apply, `tried` appends, and the FIRST pass
recorded on a number stands. An entry that repeats an item's OWN LAST applied update
exactly — same payload, whatever its line id, which a retry re-stamps — is applied
once; an item that legitimately returns to identical fields after intervening updates
is a new update and applies. An entry naming a `crew_id` other than
the fold's first is left out: every unit of one slot belongs to one crew.

**Since** — the Issue Radar crew ledger's move onto the crew log.

## The work board

### `work/recorded`

One work-board mutation: who acted, on which item, and the fields it set.

**Kind and `src`** — `session`; `src` is `gateway`.

**When written** — One entry per work-ledger write, appended to the ACTING
session's log: a conductor action (`work_ledger_record`) or a worker report
(`work_report`). The `work` fold rebuilds the board from these entries across the
conductor's and its bound workers' units, so the ledger's files are a cache of the
log rather than a record beside it; the routes still read that cache.

**Pairing** — None.

| Field | Type | Required | Meaning | Enum |
|---|---|---|---|---|
| `slot` | string | required | The board's key: the conductor slot this mutation belongs to. A worker's report names the conductor's slot, not its own, so one board folds under one key whichever party wrote the entry. | |
| `actor` | string | required | Which party wrote this entry. The field sets the two may set are disjoint. | `conductor`, `worker` (closed) |
| `by` | string | required | The acting session's slot key: equal to `slot` for a conductor entry, the bound worker's key for a report. | |
| `action` | string | required | The one mutation this entry records. The fields below are the ones that action set; an omitted field means unchanged. | `create`, `goal`, `bind`, `decide`, `verdict`, `close`, `accept`, `report` (closed) |
| `item_id` | string | optional | The item acted on. Absent only for `goal`, the board-level header write. | |
| `goal` | string | optional | The board's objective, when `goal` set one. | |
| `round` | int | optional | The board's or the item's round counter, when set. | |
| `depth` | int | optional | The board's nesting depth, carried by the first entry of a board. | |
| `parent_item` | string | optional | The parent board's item this board works, carried with `depth`. | |
| `title` | string | optional | The item's title, set by `create`. | |
| `acceptance` | object | optional | The acceptance criteria, set by `create` or `accept`. Its members are the caller's and are checked for shape by the writer. | |
| `state` | string | optional | The item's new state, set by `close`. | `open`, `accepted`, `rejected`, `abandoned` (closed) |
| `verdict` | string | optional | The acceptance verdict, set by `verdict`. | `pass`, `fail`, `pending`, `refused`, `error` (closed) |
| `decision` | string | optional | The conductor's decision text, set by `decide`. | |
| `worker_session_key` | string | optional | The worker slot bound to the item, set by `bind`. | |
| `fails` | int | optional | The item's failed-verdict count, when it moved. | |
| `status` | string | optional | The worker's status, set by `report`. | `progress`, `done`, `blocked`, `question` (closed) |
| `summary` | string | optional | The worker's summary, set by `report`. | |
| `artifacts` | object | optional | String-to-string pointers replacing the item's map, set by `report`. The members are the worker's own keys and are checked for shape. | |
| `pr` | int | optional | The pull request number, set by `report`. | |
| `event` | string | optional | The one-line item event this mutation appends to the item's tail. | |
| `event_kind` | string | optional | Which kind of item event this is. Absent only for `goal`. | `create`, `bind`, `report`, `decision`, `verdict`, `close` (closed) |

**Invariants** — A delta, never a whole record: only the fields the one `action`
set are present. A conductor entry never carries a worker field and a worker entry
never carries a conductor field; the writer refuses the cross before it builds the
entry. Every item action carries its `event` and `event_kind` in the SAME entry, so
no reader can observe an item that moved without its logged reason. The board's
timestamps (`created_at`, `closed_at`, `last_report_at`) are the entries' own `time`
and are not repeated in `data`. Every entry carries `generation`, an opaque id minted when the conductor record was created: a slot reused after its board was purged mints a new one, and the fold transitions in log order (a conductor entry with a new id opens the next board, a worker entry with another id is omitted), so a rebuild never revives a purged board. An entry about an item the record has never held whole (one from before the projection) carries `baseline: true` and the whole committed item. Every entry carries the store's own event id and stamps (`event_id`, `event_ts`, `created_at`, `last_report_at`, `closed_at`), so a rebuild reproduces them rather than the append time.

```json
{"type":"work/recorded","seq":72,"time":1789000002600,"src":"gateway","data":{"slot":"dashboard:3","actor":"worker","by":"dashboard:9","action":"report","item_id":"it_0badc0de","status":"progress","summary":"scoped tests green, opening the PR next","artifacts":{"branch":"feat/x","pr":"123"},"pr":123,"event":"progress: scoped tests green","event_kind":"report"}}
```

**Reader hint** — One board spans several logs: the conductor slot's units and each
bound worker slot's units. Fold them all, oldest unit first, and key items by
`item_id`; a report seen before its item's `create` belongs to a unit the reader has
not folded yet, not to a missing item.

**Since** — the change that made the work ledger a projection of the crew log.

## Removed types

These nine are owned by the `session` kind in the format and have no producing
site, so they are removed rather than kept as unwritten declarations.
`message/steered` sits here even though its emitter function exists, because nothing
calls it. A reader needs no handling for anything in this table.

| Type | Why it is removed |
|---|---|
| `session/seeded` | Specified for a legacy-transcript import path that no code performs. |
| `message/steered` | No single site observes both the interrupted text's flush and the steer echo, so any one writer would record a `seq` that contradicts causality. What the cut site can prove is already recorded by `message/sent.interrupted` and the requeue's `message/queued`. |
| `tool/searched` | A tool search reaches the gateway as an ordinary tool-call frame, so it is already recorded as `tool/called` and `tool/completed`. A second entry would put one fact at two `seq`s, and its query and hit count would break the rule that arguments are digested, never recorded. |
| `tool/loaded` | Its token-cost field has no source anywhere in the repository. |
| `skill/searched` | A skill search is an MCP tool call, already recorded. |
| `skill/loaded` | Reading a skill file is already a tool call, and its token field has no source: the loader counts characters. |
| `summary/written` | Its coverage field names crew log `seq`s, and the summary path holds nothing that can name one, so the field would have to be fabricated. |
| `remote/placed` | Its provider and id fields have no source at the placement site. |
| `remote/lost` | Its reason field has no source at the relay site. |
