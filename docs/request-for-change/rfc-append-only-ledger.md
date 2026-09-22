---
title: Append-only ledger — one record per unit, every view a fold
status: partial
revision: v2
author: mingweic, with Kiro
created: 2026-09-11
last-audited: 2026-09-22
audited-at: 80bd0a81f
doc-pr: 10090
implementation-prs: [10091]
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Append-only ledger — one record per unit, every view a fold

> **Naming:** the mechanism this RFC specifies is called the **crew log** (agreed
> with Joe Guo, 2026-09-17). Where the text below says "ledger" for the per-unit
> append-only file, read "crew log". The RFC's file name and title are left as they
> were so external links keep resolving; only the name of the thing changed.

Status: partial. The `kiro_crew.crew_log` store, session emitter, projections,
routes, and message entries are on main behind `KIROCREW_CREW_LOG`. The flag
remains opt-in, while the legacy transcript and conductor work-ledger stores
still exist, so the full cutover described below is incomplete.

## 1. Introduction: five heads, no history

Five stores write the same kind of fact today: the members' JSON files, the crew store's
JSON files, the work ledger, the session ledger, and a lifecycle event schema with no
writer. Each is a head with no history, and heads disagree. This design rewrites the
crew's activity record as its one append-only ledger and makes every other view a fold
over it.

Two units. A **crew** has an activity ledger; a **session** has a ledger. A crew's entry
is a sentence plus a **pointer** into a session's: "I opened PR-4127" and where to read
it. The security event log stays as the global audit log beside both. Vocabulary follows
the code (`session_ledger.py`, `work_ledger.py`): a **ledger** is the append-only file, a
**projection** a head folded from it — the word the dashboard frames use.

## 2. Requirements

- FR-1 One file per unit; the gateway is the only writer; `seq` is contiguous.
- FR-2 A line is never rewritten; a correction is a new line.
- FR-3 Every type belongs to one unit; a guest writes only in its namespace
  (`crew:<name>/…`) — a secretary in its conductor's ledger under the same rule as
  `crew:<child>/report`.
- FR-4 Any line may carry `ref`, resolved at read time page by page; no fold reads more
  than its own ledger.
- FR-5 A projection folds one ledger and carries its `seq` as version; a reconnect
  truncates.
- FR-6 A phase change without a reason is a defect (Crew Mode §7).
- FR-7 Safe to surface: no secrets, machine paths or host names in public fields; a `ref`
  instead. Message BODIES are in scope and are written, because they are redacted before
  they reach the file -- exfiltration URLs then credentials, in the writer rather than at
  the call sites, so a new call site cannot forget -- and a redaction that fails yields the
  empty string, never the input. Bodies on disk are GATED: `KIROCREW_CREW_LOG` may
  not default to on until session trash and permanent delete reach a session's ledger
  directory and `StorageReport` counts its bytes. Until both land, "delete this
  conversation" would not delete it and the disk-use surface would understate it, which are
  product promises rather than costs. Tracked as kirodotdev/KiroCrew#10705.
- FR-8 Cold load synthesizes closers for open intervals.
- NFR-1 Cheap to fold: checkpoints on disk; state never replays everything.
- NFR-2 The backend extracts, the frontend loads pages; no client folds a ledger.

## 3. Files and envelope

```
<data home>/ledgers/crews/<crew>/ledger.jsonl               the crew's activity ledger
<data home>/ledgers/crews/<crew>/projections/<key>.json     fold checkpoints, disposable
<data home>/ledgers/sessions/<id>/ledger.jsonl              the session ledger
```

Line 1 is the header; then:

```json
{"type": "crew:qa/report", "seq": 388, "time": 1789000000000, "src": "crew:qa",
 "thread": 120, "ref": {"unit": "session", "id": "s-7f3a", "from": 40, "to": 96},
 "data": {...}}
```

There is one event system, not two. The entry is field-compatible with
`kiro_crew.events`: `type` is that envelope's `kind`, `time` is its `ts_ms`, and the unit
id in the path plays its `key` — same `domain/action` naming, same epoch-millisecond
clock, so one projection can fold both streams on one axis. This is therefore the stream a
future emitter writes when it needs order, threading or citation, and `seq` is what it
adds: per unit, one contiguous sequence inside one file, assigned by that file's single
writer.

`type` is `domain/action`; `seq` and `time` are assigned at append; `src` names the emitter
(`gateway`, `acp`, `dashboard`, `patrol`, `session:<id>`, `crew:<name>`, `app:<name>`);
`data` is the fact, and a serialized entry is capped at 64 KiB. `thread` groups: it is the
`seq` of an earlier entry in the same file, the message that started the work, like a chat
thread id. `ref` is a permalink into a ledger segment, `{unit, id, from, to}`.

The header keeps its own spelling, because it describes the file rather than something
that happened. A crew ledger opens with `{type: crew, version, id, createdAt}` and nothing
more: a crew's name and template belong to the members store, which can change them, and
an append-only line cannot. A session ledger adds `owner`, `agent` and the optional
`task`, `pack`, `slot`, `thread: {crew, seq}`, `cwd` and `remote`. `owner` never changes.

## 4. Event families

Crew activity ledger:

| family | types | pointer |
|---|---|---|
| shipped | `member/*`, `activity/record`, `slot/*`, `patrol/*` | `slot/*` → the session |
| messages | `message/received`, `message/sent` | redacted body in the ledger; transcript position via `ref` once the bridge lands (not written in PR 1) |
| tree | `crew/child-attached`, `crew/parent-attached`, `*-detached` | — |
| signed | `crew:<parent>/dispatch`, `crew:<child>/report` | report → the child's segment |
| topics | `crew/topic-*`, `crew/forwarded`, `crew/run-state` | topic → its work session |
| items | `item/phase {from,to,reason}`, `item/next`, `item/probe`, `item/verdict`, `crew/round-*` | probe, verdict → evidence |
| knowledge | `crew/finding`, `crew/summary`, `crew/note-*`, `crew/link` | the segment covered |
| memory | `memory/bound\|copied\|forgotten\|restored` | — |

Messages are entries, so the chat surface is a fold: the direct message is the entries
with no `thread`; a thread page is those whose `thread` is that anchor; a permalink reply
is a `message/sent` whose `ref` points at the anchor; and "also sent to the direct
message" is a summary with no `thread` whose `ref` points at the thread.

Session ledger: `session/opened|closed`, `turn/started|completed {usage, credits}`,
`step/*`, `tool/*`, `approval/requested|decided`, `model/selected`, `compaction/applied`,
`remote/placed|lost`. Bodies are DUAL-WRITTEN into the session ledger: each is redacted,
then written whole, or split across `message/chunk` entries the citing entry names in
`chunks` when it cannot fit one line. The transcript file remains the authoritative read
path until the seeding step (`session/seeded`) imports it and the file stops being written.
For that bridge period the `ref` on a message entry is to point at its transcript position
so the two records can be reconciled while both exist -- NOT YET WRITTEN: PR 1 emits the
body alone, and `ref` and `session/seeded` arrive with the bridge (see Delivery). A
consumer must not be built against the reconciliation contract until then.

## 5. Projections and pages

Every projection folds one ledger:

| projection | ledger | value |
|---|---|---|
| `roster`, `activity`, `wake`, `driving` | crew | as today; `activity` served in pages |
| `tree`, `topics`, `items`, `board`, `budget`, `attention` | crew | children; work trees; item heads; latest report per child; credits from reports; items blocked or overdue |
| `context` | crew | what a session is given each turn: findings, summaries, notes, links, latest reports |
| `status`, `usage`, `timeline`, `tools`, `approvals` | session | the session side panel |

`board` and `budget` need no child ledger: a report carries status and credits; its `ref`
is the drill-down. Reading a team is paging: a report's `ref` loads a page of the child's
activity, whose `ref`s load pages of session ledgers.

```mermaid
flowchart LR
    M[manager activity] -->|ref| C[member activity]
    C -->|ref| S[session ledger page]
    M --> F[fold] --> B[board]
    classDef store fill:#ccfbf1,stroke:#0d9488,color:#134e4a
    class M,C,S store
```

The dashboard splits the same way: the backend folds and cuts pages
(`/crews/<id>/activity?before=<seq>&limit=`, `/sessions/<id>/ledger?from=&to=`) and pushes
`member_projection` and `session_projection` frames; the frontend renders and pages, never
folds.

## 6. Grants and visibility

Resolution has two outcomes, `ok` and `gone` — a purged target is `gone`. There is no
`forbidden` yet, because this layer has no permission model to deny with and a check with
nothing behind it only looks like a boundary. It arrives with the first caller that has
one, and the grants with it: an attach lets the parent resolve into the child's ledger, a
dispatch into the dispatched session. Each type carries a visibility class (`public`,
`tree`, `owner`); a filtered line leaves a tombstone so `seq` stays contiguous.
`member/binding`, `member/rules` and `turn/started` are `owner`. Below all of that,
`ledgers/` carries the same sandbox deny as the work ledger — only the gateway process
reads or writes it — so in-sandbox code can neither forge an entry attributed to the
gateway nor rewrite the history a conductor is meant to trust.

## 7. Migration

Activity ledger: `members/<slug>/activity.jsonl` is rewritten into the envelope at
`ledgers/crews/<crew>/ledger.jsonl`, with `seq` assigned in file order. Crew store: fold
its JSON files into `topics` lines once, rename them `.migrated`, route `CrewStore` through
the ledger. Work ledger: `items/<id>.jsonl` lines become `item/*` events,
`items/<id>.json` the `items` checkpoint; the `work_*` tools keep their surface. Sessions:
a ledger from `session/opened`.

## 8. Delivery

**PR 1** ([#10091](https://github.com/kirodotdev/KiroCrew/pull/10091)) is the whole storage
layer, for both kinds: the files, the envelope with `src`, `thread` and `ref`, the headers,
type ownership, guest namespaces, `get` and `iter_from`, page by `seq`, page by `thread`,
`resolve` with `ok`, `gone`, `pruned` and `corrupt` -- retention and damage are separate
answers, because a reader told the lines were pruned stops looking -- torn-tail repair that closes an open interval on open with a
deterministic closer — an interrupted turn gets `turn/completed {stop_reason: interrupted}`
stamped at the last real entry's time, so two readers of the same bytes agree — and the
OS-masked `ledgers/` leaf. Its first writer ships with it: the session emitter behind
`KIROCREW_CREW_LOG=1`, default off, INCLUDING message bodies. The `ref` back-pointer
to a transcript position and the `session/seeded` import are migration steps and follow;
until then the transcript file is still written and still the read path. There is no
streaming-delta emitter: redacting one delta at a time cannot see a credential split across
two of them, and `message/chunk` is written only by the oversize-body split, over text
already redacted whole. A session-kind entry carries its turn identity in
`data.turn` (and `data.step`), known at emit time, and leaves `thread` unset.

**PR 2** is projections and checkpoints, the dashboard paging endpoints and frames, the
direct-message and thread UI, the crew writer that rewrites the activity ledger, migration
of the crew store's JSON files and of the work ledger, and the secretaries.

## 9. Open questions

1. Page size and checkpoint cadence.
2. Session ledger retention; a crew ledger is never deleted, its pointers may resolve to
   `gone`.
3. An `ignorable` marker so a reader refuses to reconstruct on an unknown required type.
4. App and cron kinds: same envelope, emitters not landed.

## Amendment 2026-09-16 — the layering

Three corrections to the sections above, from implementing them. The model is one base
envelope plus session-only and crew-only halves, and the sections were written with the two
halves mixed. `docs/system-specs/modules/crew-log-core.md` sections 4, 4a, 4b and 6 are the
current specification of all three.

**The signed family is two plain crew-owned types.** §4's `crew:<parent>/dispatch` and
`crew:<child>/report` are `crew/dispatch` and `crew/report`. A type carries the fact; the
writer is `src`, so `crew:qa` reporting and `crew:docs` reporting write one type into one
parent ledger and are told apart by who signed them. Spelling the writer into the type would
make the same fact a different type per writer -- a fold would parse the type to group two
children's reports on one item, and the ownership registry would grow an entry per crew. The
two contracts carry required fields: `crew/dispatch` needs `data.target`, because a dispatch
naming nobody is a row the `board` fold cannot place, and `crew/report` needs a `ref` into
the child's segment, because `board` and `budget` take status and credits off the report
without opening the child's ledger and the `ref` is what makes that checkable. Both are
stated in section 4b.

**Authorization hangs off `src`, not off the type prefix.** FR-3's "a guest writes only in
its namespace" holds for an app, whose `app:<name>/` type prefix is the one guest type
namespace and its whole permission. A guest CREW is authorized by its `src` instead: it
writes the crew kind's built-in domains, and `crew:<name>/<action>` is refused as a
malformed type. This layer still checks no relationship — it has no crew tree, so whether
`crew:qa` is really a child of the ledger it writes into arrives with the grants in §6.

**`src` is validated per kind.** §3 lists the emitters as one set for both kinds. They are
two: a session ledger takes `gateway` and `acp`, a crew ledger takes `gateway`,
`dashboard`, `patrol`, `crew:<name>` and `app:<name>`. One shared list accepts `patrol`
inside a single session's turn history, and `src` is what a reader attributes an entry to.
`session:<id>` is accepted by neither kind: no emitter writes it, and adding a source to a
kind is additive, since no reader validates `src`.

## Amendment 2026-09-16 — the vocabulary trim

Three corrections to the sections above, from implementing them. The body is left as
written; where the two disagree, this section is what holds.

**The session vocabulary is nine types shorter.** §4 and the storage spec named types with
no site that could honestly produce them, or that would record a second time a fact another
entry already carries. Gone: `session/seeded`, `message/steered`, `tool/searched`,
`tool/loaded`, `skill/searched`, `skill/loaded`, `summary/written`, `remote/placed`,
`remote/lost`, and `digest` as a `background/completed.kind`. The vocabulary is therefore a
statement about what the log contains rather than about what it might one day contain,
which is what makes a reader's declared type set worth anything. Any of them returns when a
real source exists. Three domains empty out with them and leave `TYPE_OWNERSHIP`: `skill`,
`summary`, `remote`. The `remote` HEADER field is unrelated and stays.

**The parallel lifecycle-event stream is retired, not coexisting.** §1 counts "a lifecycle
event schema with no writer" among the five heads this design replaces, and §3 records that
the two envelopes are field-compatible so one projection could fold both. That package had
no emitter, no reader and no directory on any host, and each of its kinds names a fact this
format owns as a `type` -- so it is deleted rather than kept as a second vocabulary every
future emitter would have to choose between. A fact with no unit to belong to (a script
cron, gateway lifecycle) gets a `gateway`-kind ledger when something needs to record one.

**The format is pre-release, not frozen.** The storage spec froze the wire format in the
commit that introduced it. `KIROCREW_SESSION_LEDGER` defaults off and no user data exists on
disk, so a shape change breaks nothing and the freeze bought only the appearance of one. The
freeze point is the release that turns the flag on by default: from there a reader may hold
files, so that change is the one that decides the compatibility strategy -- additive fields
plus the `ignorable` skip, or migrations -- and `version` is the escape hatch it spends.
