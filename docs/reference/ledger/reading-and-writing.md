# Reading and writing

**Local page, not a mirror.** Part of the [ledger reference](README.md), which is
marked as a named exception in [the Reference index](../README.md).

Every name here is from `kiro_crew.ledger`. Each refusal names a code from
[errors.md](errors.md).

## Opening and creating

`Ledger` is the handle. Both entry points are classmethods.

The subagent-aware repair on this page -- the `child_gone` predicate on `Ledger.open`
and `repair_interrupted_turn`, and the `approval/decided` and `subagent/failed`
closers -- lands with #11185. On a build without it, `open` and
`repair_interrupted_turn` take no `child_gone` argument and repair writes the
`tool/completed` and `turn/completed` closers only.

| Call | Signature | Refuses with |
|---|---|---|
| `Ledger.exists` | `(kind, unit_id) -> bool` | `bad_kind`, `invalid_id`, `bad_root` |
| `Ledger.create` | `(kind, unit_id, **header_fields) -> Ledger` | `bad_kind`, `invalid_id`, `bad_root`, `already_exists`, `bad_header_field`, `bad_data`, `entry_too_large` |
| `Ledger.open` | `(kind, unit_id, *, repair=False, child_gone=None) -> Ledger` | `bad_kind`, `invalid_id`, `bad_root`, `no_ledger`, `bad_header`, `unsupported_version`, `already_owned` |

All three resolve the unit's path before doing their own work, so all three can refuse
`bad_kind`, `invalid_id` and `bad_root` first; the codes after those are what each call
adds. `create` reaches `bad_data` and `entry_too_large` because it serializes a header
line, which every later append is measured against the same way.

`exists` asks whether the unit has any segment carrying content; a zero-byte file
counts as absent.

`create` writes the header atomically — temp file, fsync, rename — and refuses a
unit whose file is already there. The check runs twice, the second time under the
lock, so two racing creates cannot both win. The returned handle has `last_seq` 0.

`open` reads the header from the oldest segment and positions on the newest one
carrying content. It **always** truncates a torn tail, whether or not repair is
asked for. With `repair=True` it additionally takes write ownership *before*
running closers, so it can raise `already_owned`; if anything then fails, the lease
is released rather than leaked. `child_gone` is a predicate the subagent closer
consults — see [crash repair](#crash-repair).

`repair_interrupted_turn(*, child_gone=None) -> int` runs the same repair on a
handle already held and returns how many closers it wrote.

Read-only helpers at module level: `ledger_root(kind)`, `ledger_dir(kind, unit_id)`,
`ledger_path(kind, unit_id)`, `segment_paths(kind, unit_id)`,
`segment_first_seqs(kind, unit_id)`, and `now_ms()`. Handle properties: `kind`,
`id`, `path`, `header`, `last_seq`.

## Reading

### `iter_from`

`iter_from(seq=1, *, known=None) -> Iterator[Entry]` yields every entry from `seq`
onward, oldest first, across segments.

`known` is the whole point of the call. It declares the types this reader
understands, and it changes what happens when the reader meets one it does not:

- `known=None` — yield everything. No type is refused. This is the right choice for
  a tool that copies, counts or exports lines without interpreting them.
- `known={…}` — an entry whose type is not in the set is **skipped if and only if
  it is marked `ignorable`**. Otherwise the iterator raises
  [`unknown_entry_type`](errors.md#unknown_entry_type) and reconstruction stops.

Stopping is deliberate. A required entry a reader cannot interpret may change the
meaning of every entry after it, so a reader that folds state must not be handed a
partial history that looks complete. `ignorable` is the writer's promise that
nothing later depends on this line having been read, which is what lets an older
reader keep folding a file a newer writer extended.

Crossing segments can also refuse: [`bad_segment`](errors.md#bad_segment) for a
segment whose header or declared first `seq` does not match, and
[`segment_gap`](errors.md#segment_gap) for a gap between two surviving segments. A
first segment starting above 1 is a pruned front and is read without complaint.

### `get`, `page`, `thread_page`

`get(seq) -> Entry | None` returns the entry at one `seq`, or `None`. It stops once
the stream passes `seq`, so it does not read the whole file.

`page(before=None, limit=DEFAULT_PAGE_LIMIT) -> Page` returns up to `limit` entries
**older** than `before`, exclusive, newest first — the shape a UI scrolling
backwards wants. `limit` is clamped to `[1, MAX_PAGE_LIMIT]`; the default is 50
(`DEFAULT_PAGE_LIMIT`) and the cap is 500 (`MAX_PAGE_LIMIT`). There is no `known` gate
on this path: paging is for display, not
for folding state.

`thread_page(anchor, before=None, limit=DEFAULT_PAGE_LIMIT) -> Page` returns one
thread — the entries whose `thread` is `anchor`, plus the anchor itself — newest
first with the anchor last.

### Resolving a `ref`

`resolve(ref) -> Resolution` follows a citation. A same-unit ref resolves against
the open handle; anything else opens the cited unit. The result is a `Resolution`
carrying a `status` and a tuple of `entries`, with `.ok` true only for `ok`.

It answers with a status rather than raising, because a cited ledger being gone is
a normal outcome of a long-lived pointer, not a bug in the reader:

| Status | Meaning |
|---|---|
| `ok` | Every cited `seq` was read back: the covered seqs are distinct and their count matches the span. |
| `gone` | The cited ledger does not exist at all. |
| `pruned` | The span reaches below the oldest surviving segment's first `seq`, and what survives is intact. |
| `corrupt` | The ledger exists but will not open, or the read was short, or a seq repeated, or the span reaches past the newest entry. A damaged segment chain is reported here rather than raised. |

Two things `resolve` does **not** do. It makes no authorization decision and takes
no access callback, so there is no `forbidden` status: a caller that needs a
permission model applies it around this call. And it does not enforce the span cap —
that is checked when the `ref` is constructed, raising
[`bad_ref`](errors.md#bad_ref) for a span wider than 500 lines.

## Writing

### Type ownership

A kind owns a set of `type` domains, matched by prefix so a new action under an
owned domain needs no registration. Writing a type the kind does not own is
[`event_type_not_owned`](errors.md#event_type_not_owned). The session kind's
domains are on [session-types.md](session-types.md); the crew kind's are on
[crew-types.md](crew-types.md).

### Guest namespacing

A guest writer is one whose `src` carries its own name — `crew:<name>` or
`app:<name>`. That name is its permission: an `app:<name>` writer may write only
`app:<name>/…` types, and only into a crew ledger. Writing outside its own
namespace is [`namespace_violation`](errors.md#namespace_violation). An `src` that
is neither a known fixed emitter nor a well-formed namespaced one is
[`bad_src`](errors.md#bad_src).

### Ownership

One writer owns a unit's ledger at a time, held as an advisory lock on the unit's
`.lease` file.

It is taken **non-blocking**: a writer either owns the file or is told it does not,
and never queues. Within one process the lease is refcounted by the lease file's
path, so several handles on the same unit share it and are not in contention with
each other; the last release closes the descriptor. After locking, the held inode is
compared with the file now at that path, retried a small fixed number of times, so a
lease file replaced underneath a writer is reported as contention rather than
silently trusted.

There is **no expiry**. Ownership lasts as long as the handle can still append — it
is bound to the handle's lifetime by a finalizer — which means a wedged but live
writer keeps ownership until its process exits. That is the intended trade: a
timeout would let a second writer start appending to a file the first one may still
be writing.

When another **process** owns the ledger, an append raises
[`already_owned`](errors.md#already_owned) and writes nothing. This is a hard
refusal, not a retry hint: ownership is held for the life of the owning process, so
every later entry for that unit would queue behind the same wait. A caller reports
the loss instead.

### `append`

`append(type, data, *, src, thread=None, ref=None, ignorable=False) -> Entry`.

Validation happens **before any byte is written**, in this order: `data` must be a
JSON object of serializable values ([`bad_data`](errors.md#bad_data)); the type must
be well formed and owned, and the `src` acceptable
([`bad_type`](errors.md#bad_type), [`bad_src`](errors.md#bad_src),
[`event_type_not_owned`](errors.md#event_type_not_owned),
[`namespace_violation`](errors.md#namespace_violation)); a `thread` must name an
existing, earlier, readable entry in this same file
([`bad_thread`](errors.md#bad_thread)); the serialized line must fit the ceiling
([`entry_too_large`](errors.md#entry_too_large)).

Then ownership is claimed and the tail is re-scanned under the unit's `.lock`.
`seq` is assigned as `last_seq + 1` where `last_seq` is read back from the file's
own tail window on **every** write, never trusted from the cached property. `time`
is assigned by the writer. So a refusal always means nothing happened, and a return
always means the line is on disk and fsynced.

### `append_many`

`append_many(items, *, src, cite=None) -> list[Entry]` writes a group in one write
and one fsync. An empty list returns an empty list. Every item's `data` and the
ownership are validated up front, and each serialized line must fit the ceiling
individually.

`cite` is what makes an oversize body atomic. It is called **inside the lock** with
the seqs just allocated, and whatever dict it returns is appended last — so the
citing entry can name the seqs of the chunks it cites, and a reader either sees the
whole group or none of it. A `cite` that returns something other than a dict is
[`bad_data`](errors.md#bad_data) on field `cite`.

This path does not accept `thread`.

### When an append cannot be rolled back

An ordinary failure is definite: bytes reach the file before the fsync, so a failed
append truncates the file back to the size it had and re-raises, leaving nothing for
a retry to reason about.

`IndeterminateAppend` is the case where that cleanup **also** failed, which is
likely, since whatever broke the write is often still broken. The file may hold
bytes no entry claims. It is deliberately not a `LedgerError`: a `LedgerError` means
the entry was declined before any byte was written, and this is the opposite. The
residue is an unterminated or unparseable tail, which is exactly the shape the next
`open` truncates, so the recovery already exists — the distinct type is so a caller
can tell "nothing happened" from "something may have".

### The fail-soft emitter

The storage layer refuses. It does not retry, and it does not decide what a lost
write means.

That policy lives in the session emitter above it, which is where the write-behind,
the loss accounting and the [`write/dropped`](session-types.md#writedropped) marker
belong. The emitter's contract is that a session is never slowed or failed by its
own ledger: a write it cannot land is dropped and accounted for, and the count and
byte total surface as one `write/dropped` entry at the head of the next batch.
Further loss merges into the same pending marker rather than adding entries. Because
a lost job never took a `seq`, the loss leaves `seq` contiguous — which is why
`write/dropped` is the only thing that can tell a reader the record is incomplete.

## Crash repair

Repair is deterministic: the same file always repairs to the same bytes.

**Torn tail — every `open` and every `append`.** Trailing unterminated bytes are
classified. Bytes that do not parse are dropped. Bytes that parse whole are kept and
the next append re-supplies the missing newline. An interior line that is
newline-terminated but unparseable is left on disk and skipped on read, because
truncating at it would discard everything after it.

**Interrupted-turn closers — session ledgers only, and only with `repair=True`.**
One closer per still-open opener, written in this order with a fixed value per type:

| Open opener | Closer written | Fixed value |
|---|---|---|
| `approval/requested` | `approval/decided` | `decision: "unknown"` (#11185) |
| `tool/called` | `tool/completed` | `status: "unknown"` |
| `subagent/spawned` | `subagent/failed` | `outcome: "unknown"` (#11185) |
| `turn/started` | `turn/completed` | `stop_reason: "interrupted"` |

Every closer is written with `src` `gateway` and reuses the **last real entry's
`time`** rather than the current clock, so repair does not invent a timestamp for
something that happened before the crash. Seq continues from the tail.

Two guards keep repair from writing over uncertainty. A trailing orphan
`message/chunk` run whose citing entry never landed is truncated *before* any closer
is written. And if the fold that decides what is open skipped any record at all —
blank, malformed, or unreadable — repair writes **nothing** and reports zero
closers, rather than closing openers based on a history it could not fully read.

`subagent/spawned` is the one opener repair will leave open. It is closed only for a
child the `child_gone` predicate reports finished; with no predicate supplied the
spawn stays open, because a running child must not be recorded as failed.

## Segments and retention

The read side is built for many segments: `segment_paths` lists them ascending by
first `seq`, each is validated against the unit it belongs to, and `seq` contiguity
is checked across every boundary. A pruned front — an oldest segment starting above
1 — is a supported state that `iter_from` reads and that `resolve` reports as
`pruned`.

The write side creates one segment. No code in `kiro_crew.ledger` rolls a new
segment or prunes an old one, so `ledger.jsonl` is the only segment a writer
produces. Retention removes whole segments off the **front**, which is why a front
gap is legal and a mid-chain gap is
[`segment_gap`](errors.md#segment_gap).
