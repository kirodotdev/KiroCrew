# Errors

**Local page, not a mirror.** Part of the [crew log reference](README.md), which is
marked as a named exception in [the Reference index](../README.md).

Every refused crew log operation raises `CrewLogError` carrying a stable `code`, and
`field` naming the offending input when one field is to blame. A caller branches on
`code`; the human-readable message is free to change without breaking it.

**Codes are additive-only.** A shipped code string is never renamed or repurposed —
it is API surface, not a log string. A new refusal gets a new code.

A `CrewLogError` means the operation was **declined before any byte was written**. For
the one case where a write may have left residue, see
[`IndeterminateAppend`](#indeterminateappend).

## Summary

| Code | Path | Trigger |
|---|---|---|
| [`bad_kind`](#bad_kind) | both | Requested kind is neither `crew` nor `session`. |
| [`invalid_id`](#invalid_id) | both | Unit id is empty, holds a separator or NUL, or escapes its root. |
| [`bad_root`](#bad_root) | both | A kind's root under `crew-log` is a link or resolves outside the data home. |
| [`already_exists`](#already_exists) | create | `create` was asked for a crew log whose file is already there. |
| [`no_ledger`](#no_ledger) | open, append | No content-bearing segment exists. |
| [`already_owned`](#already_owned) | write | Another process owns writes to this unit. |
| [`bad_header`](#bad_header) | read | Line 1 is missing, unparseable, or names a different unit. |
| [`bad_header_field`](#bad_header_field) | create | A header field is missing, wrongly typed, or unknown. |
| [`unsupported_version`](#unsupported_version) | read | The header names a format version this build does not read. |
| [`bad_type`](#bad_type) | write | `type` is not spelled `domain/action`. |
| [`bad_src`](#bad_src) | write | `src` is neither a fixed emitter nor a well-formed namespaced one. |
| [`bad_data`](#bad_data) | write | `data` is not a JSON object, or holds something unserializable. |
| [`event_type_not_owned`](#event_type_not_owned) | write | This kind does not own that `type` domain. |
| [`namespace_violation`](#namespace_violation) | write | A guest wrote outside its own namespace. |
| [`bad_thread`](#bad_thread) | write | `thread` names no existing earlier entry in this file. |
| [`bad_ref`](#bad_ref) | write | `ref` is malformed or spans too many lines. |
| [`entry_too_large`](#entry_too_large) | write | The serialized line is over the size ceiling. |
| [`unknown_entry_type`](#unknown_entry_type) | read | A declaring reader met a type it does not know that is not `ignorable`. |
| [`bad_segment`](#bad_segment) | read | A segment's header or declared first `seq` does not match. |
| [`segment_gap`](#segment_gap) | read | `seq` is not contiguous across a segment boundary. |

## Identity and location

### `bad_kind`

The requested kind is neither `crew` nor `session`.

**Caller action** — A programming error. Fix the call; there is no runtime recovery.

### `invalid_id`

The unit id is empty, carries a path separator or a NUL, or resolves outside its own
root.

A unit id names a directory, so a separator in it would let the crew log escape its
root. The raw id is refused loudly rather than folded to something safe: a fold would
silently merge two units into one file, and two units sharing a crew log is worse than
a refused write.

**Caller action** — Refuse the operation upstream. Do not sanitize and retry.

### `bad_root`

A kind's root directory under `crew-log` is a link, or resolves outside the data
home's own crew log tree. The message names the directory.

Containment resolves its base first, so a linked kind root would make the link's
target the containment root and every path under it would pass — private history
readable and forgeable at a location the data home's protections do not cover.

**Caller action** — Operational, not programmatic. Surface it for an operator to
inspect what the directory points at. Do not create or replace it automatically.

## Lifecycle

### `already_exists`

`create` was asked for a crew log whose file is already there. Field: `id`.

**Caller action** — Call `open` instead. The check runs twice, the second time under
the lock, so this code also means a racing create won.

### `no_ledger`

`open` was asked for a crew log with no content-bearing segment. Field: `id`.

**Caller action** — Call `create`, or treat the unit as having no history. When it
comes from resolving a `ref`, it surfaces as the `gone` status rather than as this
error.

### `already_owned`

Another process owns writes to this unit's crew log, so this one appended nothing.

Distinct from every shape code: the entry was well formed and the file is healthy,
this process is simply not the writer.

**Caller action** — **Report the loss; do not retry.** Ownership is held for the life
of the owning process, so every later entry for that unit would queue behind the same
wait. In the session emitter this is one of the paths that ends in a
[`write/dropped`](session-types.md#writedropped) marker.

## Header

### `bad_header`

Line 1 is missing, unparseable, or describes a different unit than the one asked
for. A crew log without a readable header is not a crew log. Also raised for a non-integer
`createdAt` or `version`.

**Caller action** — Treat the file as damaged. Quarantine it rather than appending
to it.

### `bad_header_field`

A header field is missing, of the wrong type, or unknown. Building a header refuses
an unknown key; reading one ignores unknown keys, so this is a write-path code.

**Caller action** — Fix the call. A malformed session `thread` raises this when a
header is built, but is folded to absent when one is read — a damaged back-pointer
costs the pointer, not the file.

### `unsupported_version`

The header names a format version this build does not read.

Kept distinct from `bad_header` on purpose: the file is not damaged, this process is
old. A newer version may legitimately fail every structural check, so the caller has
to be told to upgrade rather than told the log is corrupt.

**Caller action** — Report that an upgrade is needed. Do not truncate, repair or
append.

## Entry shape

### `bad_type`

`type` is not spelled `domain/action`. Field: `type`.

The split is on the first slash, and the action may not contain one, so a namespace
is exactly one level deep and no action can smuggle a second domain behind it.

**Caller action** — Fix the call.

### `bad_src`

`src` is neither one of the fixed emitters nor a well-formed namespaced one. Field:
`src`.

**Caller action** — Fix the call. The values each kind accepts are on
[envelope.md](envelope.md#entry-fields) and
[crew-types.md](crew-types.md#allowed-src).

### `bad_data`

`data` is not a JSON object, or holds something not JSON-serializable. Also raised on
field `cite` when an `append_many` citation callback returns something other than a
dict.

**Caller action** — Fix the call. This is the code a per-type payload validator
reports through, so it may also mean a payload that is a valid object but does not
match its type's contract.

### `event_type_not_owned`

This crew log kind does not own that `type` domain. The message names the domains the
kind does own.

**Caller action** — Write to the right kind of crew log, or use the right type. See
[crew-types.md](crew-types.md#the-layering-rule).

### `namespace_violation`

A guest emitter wrote outside its own namespace, or wrote a guest type into a session
log.

**Caller action** — A guest's own name is its permission. Write `app:<name>/…` types
as `app:<name>`, into a crew log.

### `bad_thread`

`thread` does not name an existing, earlier `seq` in this same crew log — it is past
the newest entry, or names nothing readable. Field: `thread`.

**Caller action** — Anchor on a `seq` this crew log returned. `thread` is never a
pointer into another unit; that is `ref`.

### `bad_ref`

`ref` is malformed: a bad unit, a bad id, a non-positive bound, an inverted bound, or
a span wider than 500 lines (`MAX_REF_SPAN`). Fields: `unit`, `ref.id`, `ref.from`,
`ref.to`.

Raised when the ref is **constructed**, not when it is resolved, so an unresolvable
ref and an unrepresentable one are different problems.

**Caller action** — Cite a narrower span. A citation is a pointer, not a bulk export:
an unbounded span would let one entry make its reader materialize a whole history.

### `entry_too_large`

The serialized entry line is over the 64 KiB ceiling (`MAX_ENTRY_BYTES`).

**Caller action** — Split the payload. A body is split into
[`message/chunk`](session-types.md#messagechunk) entries by the emitter; the line is
refused, never clipped, so a reader never meets a half-recorded fact.

## Read path

### `unknown_entry_type`

A reader that declared the types it understands met one it does not, and the entry
did not mark itself `ignorable`. Field: `type`. Raised on the **read** path only.

An entry a reader cannot interpret may change the meaning of every entry after it, so
reconstruction stops rather than silently skipping it.

**Caller action** — Either teach the reader the type, or pass no `known` set if the
reader does not interpret entries at all. Do not catch this and continue folding: the
state you build past it is not trustworthy.

### `bad_segment`

A segment's header does not belong to the crew log being read, its schema version
disagrees, or its filename's declared first `seq` disagrees with its readable physical
first entry. The message names the offending segment.

**Caller action** — Quarantine the named segment. When it comes from resolving a
`ref`, it surfaces as the `corrupt` status instead of being raised.

### `segment_gap`

`seq` is not contiguous across a segment boundary: the segment after the gap does not
start where the one before it ended.

Retention removes whole segments off the **front**, which leaves no gap between the
ones that remain — so a gap here is damage or a partial copy, not a pruned log.

**Caller action** — Treat the chain as damaged. A missing *front* is not this error;
it reads normally and resolves as `pruned`.

## Not a `CrewLogError`

### `IndeterminateAppend`

An append failed **and** could not be rolled back.

The ordinary failure is definite: bytes reach the file before the fsync runs, so an
append that fails anywhere truncates the file back to the size it had and re-raises,
leaving nothing behind for a retry to reason about. This is the case where that
cleanup also failed — likely, since whatever broke the write is often still broken —
so the file may hold bytes no entry claims.

It carries `written` and `offset` rather than a `code`, and it is deliberately not a
`CrewLogError`: a `CrewLogError` is a refusal, meaning nothing was written and retrying
is pointless. Here the file has been touched and the outcome is unknown, which is the
opposite.

**Caller action** — Do not retry the entry blind. The residue is an unterminated or
unparseable tail, which is the shape the next `open` truncates, so the recovery
already exists; the distinct type is there so a caller can tell "nothing happened"
from "something may have".
