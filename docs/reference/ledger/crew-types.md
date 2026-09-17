# Crew ledger entry types

**Local page, not a mirror.** Part of the [ledger reference](README.md), which is
marked as a named exception in [the Reference index](../README.md).

> **No crew emitter exists.** The families and the two contracts on this page are
> the specified shape a crew writer must produce. The `crew/dispatch` and
> `crew/report` contracts are defined here and in
> [`ledger-core.md`](../../system-specs/modules/ledger-core.md); a validator that
> refuses a malformed one is pending. Treat every field rule below as the
> agreement, not as something the code enforces today.

A crew ledger uses the same file layout, the same header rules and the same eight
envelope fields as a session ledger — [envelope.md](envelope.md) covers all of
that. Three things differ: which `type` domains may be written, which `src` values
may write them, and the fact that `thread` and `ref` are actually used.

## The layering rule

There is one base envelope, then two disjoint sets of types on top of it. A type
belongs to exactly one kind, and the kind of the ledger decides whether a type may
be written to it at all. Writing a crew-owned type into a session ledger is refused
with [`event_type_not_owned`](errors.md#event_type_not_owned), and the reverse is
refused the same way.

The crew kind owns eight `type` domains: `member`, `activity`, `slot`, `patrol`,
`message`, `crew`, `item`, `memory`.

Two overlaps between the kinds are deliberate rather than accidental. The
`message` domain exists in **both** kinds, carrying different `data` in each:
ownership answers "does this kind have such events", and a crew and a session both
do. And `ref` **crosses** kinds — a crew entry citing a span of a session's file is
the one intended cross-kind pointer.

A type name never carries the writer's identity. Who wrote a line is `src`, not
part of `type`, so a dispatch written by one crew and a dispatch written by another
are the same type and a reader folds them together. The `app:<name>/<action>` guest
form is reserved for facts an app defines that no built-in domain covers.

## Allowed `src`

A crew entry is written by one of:

| `src` | Who it is |
|---|---|
| `dashboard` | The dashboard, acting for the person using it. |
| `patrol` | A patrol pass. |
| `gateway` | The gateway itself. |
| `crew:<name>` | A named crew, writing into its own crew ledger. |
| `app:<name>` | A named app, writing only its own `app:<name>/…` types, and only into a crew ledger. |

`dashboard` and `patrol` are crew-side values: a session entry is written with
`gateway` or `acp` and nothing else.

A guest's own name is its write permission. A `crew:<name>` writer may write
crew-owned types into that crew's ledger; an `app:<name>` writer is confined to its
own namespace. Writing outside it is
[`namespace_violation`](errors.md#namespace_violation), and an unparseable `src` is
[`bad_src`](errors.md#bad_src).

## Families

From the RFC's event-family model. Each row names the shape of a family rather than
an exhaustive type list, because a new action under an owned domain needs no
registry change.

| Family | Types | What it points at |
|---|---|---|
| shipped | `member/*`, `activity/record`, `slot/*`, `patrol/*` | `slot/*` cites the session it shipped. |
| messages | `message/received`, `message/sent` | The redacted body is in the ledger; a permalink reply cites its anchor with `ref`. |
| tree | `crew/child-attached`, `crew/parent-attached`, and their `-detached` counterparts | — |
| dispatch | `crew/dispatch`, `crew/report` | A report cites the child's segment. |
| topics | `crew/topic-*`, `crew/forwarded`, `crew/run-state` | A topic cites its work session. |
| items | `item/phase`, `item/next`, `item/probe`, `item/verdict`, `crew/round-*` | A probe and a verdict cite their evidence. |
| knowledge | `crew/finding`, `crew/summary`, `crew/note-*`, `crew/link` | The segment covered. |
| memory | `memory/bound`, `memory/copied`, `memory/forgotten`, `memory/restored` | — |

The chat surface is a fold over the `messages` family rather than a separate store.
A direct message is the entries with no `thread`; a thread page is the entries whose
`thread` is that anchor; a reply that quotes a line is a `message/sent` whose `ref`
names it.

## `crew/dispatch`

A crew handed one work item to a target.

**Kind and `src`** — `crew`; written by the dispatching party, so `src` is
`dashboard`, `patrol`, `gateway` or `crew:<name>`.

**Pairing** — Opener. Answered by one or more [`crew/report`](#crewreport) entries
that thread onto its `seq`.

| Field | Type | Required | Meaning | Enum |
|---|---|---|---|---|
| `item` | string | required | The work item's id. | |
| `target` | object | required | Who the item went to. **A dispatch without a target is invalid.** | |
| `target.kind` | string | required | Which sort of target. | `session`, `crew` |
| `target.slot` | string | conditional | The slot key, required when `target.kind` is `session`. | |
| `target.name` | string | conditional | The crew name, required when `target.kind` is `crew`. | |
| `brief` | string | optional | The brief handed over. | |

**Invariants** — `target` is required and its `kind` decides which of `slot` or
`name` must be present. The two forms are exclusive: a target names a session slot
or a crew, never both.

```json
{"type":"crew/dispatch","seq":41,"time":1789000003000,"src":"crew:qa","data":{"item":"WI-4","target":{"kind":"session","slot":"dashboard:7"},"brief":"write the reference pages"}}
```

**Reader hint** — Group a work item's history by `item`, then order it by `seq`. The
dispatch is the anchor every report threads onto.

## `crew/report`

A dispatched party reported back on one work item.

**Kind and `src`** — `crew`; written by the reporting party, so `src` is
`crew:<name>` for a crew and `gateway` for a session's own report.

**Pairing** — Closer in practice, though a `progress` status leaves the item open.
`thread` is the dispatch's `seq`.

| Field | Type | Required | Meaning | Enum |
|---|---|---|---|---|
| `item` | string | required | The work item's id, matching the dispatch. | |
| `status` | string | required | Where the item stands. | `done`, `blocked`, `failed`, `progress` |
| `credits` | number | optional | What the work cost. | |
| `summary` | string | optional | What was done. | |

**Envelope requirements** — Unlike every other type documented here, `crew/report`
constrains the envelope as well as `data`:

- **`ref` is required.** It cites the segment of the child's ledger holding the
  work, so a report is never a claim without evidence. The span cap of 500 lines
  (`MAX_REF_SPAN`) applies, so a long run is cited by its relevant span rather than
  in full.
- **`thread`** is the `seq` of the `crew/dispatch` being answered.

**Invariants** — A report without a `ref` is invalid. `status: "progress"` may
appear several times for one dispatch; a terminal status appears once.

```json
{"type":"crew/report","seq":58,"time":1789000004000,"src":"crew:qa","thread":41,"ref":{"unit":"session","id":"s-7f3a","from":12,"to":40},"data":{"item":"WI-4","status":"done","credits":1.42,"summary":"six pages and a test"}}
```

**Reader hint** — Resolve the `ref` to read the work itself, and handle all four
statuses the resolution can answer with — the cited ledger may be gone or pruned
long after the report was written. See
[reading-and-writing.md](reading-and-writing.md#resolving-a-ref).
