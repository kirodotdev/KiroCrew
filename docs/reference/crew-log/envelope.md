# Envelope

**Local page, not a mirror.** Part of the [crew log reference](README.md), which is
marked as a named exception in [the Reference index](../README.md).

Everything here is kind-independent: it holds for a session's log and a crew's
log alike. What differs between the two kinds is which `type` domains and which
`src` values are allowed, which [session-types.md](session-types.md) and
[crew-types.md](crew-types.md) cover.

## File layout

```
<data home>/crew-log/crews/<store name>/log.jsonl
<data home>/crew-log/sessions/<store name>/log.jsonl
```

`crew-log` is one shared root for both kinds, which is what lets a single fence
entry cover every unit kind at once. Under it, `crews/` holds crew units and
`sessions/` holds session units.

`<store name>` is not the raw unit id. It is a readable-prefix-plus-digest fold of
it (`session_ledger._store_name`), because a unit id may legitimately carry a colon
— a channel session key looks like `slack:1712793600.123` — and a raw id must never
decide a directory name. The raw id is recorded in the header, and the path is
re-checked for containment symlink-safely when it is built.

Each unit directory holds:

| Path | What it is |
|---|---|
| `log.jsonl` | The segment whose first entry is `seq` 1. The only segment a writer creates. |
| `log.<first seq>.jsonl` | A later segment. The filename's middle is its first entry's `seq`, all digits and greater than 1. |
| `.lock` | The append lock. Held only for the duration of one append, and it is what makes `seq` assignment serial. |
| `.lease` | The write-ownership lease. Held for as long as a handle can still append. See [reading-and-writing.md](reading-and-writing.md#ownership). |

Line 1 of a segment is a header object. Every later line is an entry. **Every
segment repeats the header**, not just the first one, so a unit whose oldest
segment is gone is still a readable crew log. A reader validates each segment's
header against the unit it opened and checks that the filename's declared first
`seq` matches the segment's first readable entry.

Reading crosses segments oldest first. A `seq` gap *between* two surviving segments
is damage ([`segment_gap`](errors.md#segment_gap)); a first segment that starts
above 1 is a pruned front and is read without complaint.

## Header fields

`type` on the header line is the kind, and `createdAt` is epoch milliseconds.
`version` is the header schema version, `1`.

Common to both kinds:

| Field | Type | Required | Meaning | Enum |
|---|---|---|---|---|
| `type` | string | required | The unit kind this file belongs to. | `crew`, `session` |
| `version` | int | required | Header schema version. A file declaring a version this build does not read is refused with [`unsupported_version`](errors.md#unsupported_version). | `1` |
| `id` | string | required | The raw unit id. Must match the id the reader opened. | |
| `createdAt` | int | required | Epoch milliseconds the unit's crew log was created. | |

A **crew** header carries nothing beyond those four: a crew's identity is its id,
and every fact about it is an entry rather than a header field.

A **session** header adds the facts fixed for the session's whole life:

| Field | Type | Required | Meaning | Enum |
|---|---|---|---|---|
| `owner` | string | required | The session's owner. | |
| `agent` | string | required | The agent the session runs. | |
| `task` | string \| null | optional | The task the session was opened for. | |
| `pack` | string \| null | optional | The pack the session belongs to. | |
| `slot` | string \| null | optional | The slot key serving the session. | |
| `thread` | object \| null | optional | Back-pointer to the crew row that dispatched this session: `{crew, seq}`. | |
| `cwd` | string \| null | optional | Working directory. | |
| `remote` | object \| null | optional | Remote placement facts. | |

The header's `thread` is the one place `thread` is an object rather than an int.
Its two wire fields are `crew` (the crew unit's id, a string) and `seq` (the
dispatching entry's `seq`, a positive int). A malformed `thread` is refused when a
header is built ([`bad_header_field`](errors.md#bad_header_field)) but folded to
absent when one is read, so a damaged back-pointer costs the pointer and not the
file.

Building a header refuses an unknown key. Reading one ignores unknown keys, which
is what lets an older build read a file a newer writer extended.

### Header example

```json
{"type":"session","version":1,"id":"s-7f3a","owner":"default","task":null,"pack":null,"agent":"kirocrew","slot":"dashboard:3","thread":{"crew":"qa","seq":41},"cwd":"/home/u/proj","remote":null,"createdAt":1789000000000}
```

## Entry fields

Eight fields, four always written and four written only when set. Field order on
the wire is fixed: the four required fields, then `thread`, `ref`, `ignorable` when
present, then `data` last.

| Field | Type | Required | Meaning | Enum |
|---|---|---|---|---|
| `type` | string | required | `domain/action`, partitioned on the first slash. The action may not itself contain a slash, so a namespace is exactly one level deep. A guest form `app:<name>/<action>` exists for crew logs. | |
| `seq` | int ≥ 1 | required | Writer-assigned under the unit's lock, contiguous from 1 after the header. | |
| `time` | int | required | Writer-assigned epoch milliseconds. | |
| `src` | string | required | Which emitter wrote the line. | `gateway`, `acp`, `dashboard`, `patrol`, or `session:<id>` / `crew:<name>` / `app:<name>` |
| `thread` | int ≥ 1 | optional | The `seq` of an earlier entry in this same file, used as a grouping key. Refused if it names no earlier readable entry ([`bad_thread`](errors.md#bad_thread)). Session emitters never set it. | |
| `ref` | object | optional | A citation into another or the same crew log. | |
| `ignorable` | `true` | optional | The writer's promise that a reader may skip this line when it does not know the type. Only a literal `true` is written; the key is absent otherwise. | `true` |
| `data` | object | required | The type-specific payload. Must be a JSON object holding JSON-serializable values ([`bad_data`](errors.md#bad_data)). | |

`src` names either a whole subsystem carrying no instance id (`gateway`, `acp`,
`dashboard`, `patrol`) or one instance (`session:<id>`, `crew:<name>`,
`app:<name>`). Session entries are written with `gateway` or `acp` and nothing
else; the values a crew entry may carry are on
[crew-types.md](crew-types.md#allowed-src).

`ignorable` is what lets an older reader keep folding a newer writer's file. It is
a claim about *dependency*, not importance: setting it says nothing later in the
file needs this entry to have been interpreted. A sampled body slice qualifies; a
closer never does.

The serialized line ceiling is 64 KiB (`MAX_ENTRY_BYTES`). A line over it is
refused with [`entry_too_large`](errors.md#entry_too_large) before any byte is
written.

### Entry example

```json
{"type":"turn/started","seq":12,"time":1789000000200,"src":"gateway","data":{"turn":3,"actor":"user","depth":0}}
```

## `ref`

A `ref` is a citation, not a copy: the bytes stay in the crew log they were written
to, and a reader resolves the pointer when it wants them.

```json
{"unit":"session","id":"s-7f3a","from":12,"to":40}
```

| Field | Type | Required | Meaning | Enum |
|---|---|---|---|---|
| `unit` | string | required | The kind of crew log being cited. | `crew`, `session` |
| `id` | string | required | The cited unit's raw id. | |
| `from` | int ≥ 1 | required | First cited `seq`. | |
| `to` | int ≥ `from` | optional | Last cited `seq`. Absent cites the single line at `from`. | |

The span `to - from + 1` may be at most 500 lines (`MAX_REF_SPAN`). Every shape
failure — an unknown unit, an unusable id, a non-positive or inverted bound, an
over-wide span — is [`bad_ref`](errors.md#bad_ref), raised when the ref is
constructed rather than when it is resolved.

Resolving a ref answers with a status rather than an exception, because a cited
crew log may legitimately be gone or pruned. The four statuses are on
[reading-and-writing.md](reading-and-writing.md#resolving-a-ref).

## `thread`

`thread` groups entries inside one file: it holds the `seq` of an earlier entry in
the same crew log, and a reader pages a group by that anchor. It is validated
against the file at append time, so it can never name a line that does not exist
or one that comes later.

Session entries never carry it. It is the crew's log's shape, where a report
threads onto the dispatch it answers.
