---
title: Remove a saved workflow -- a tombstone takes it out of every list and run path, and its revisions, lineage and slug stay
status: in-progress
author: jeeshofone, with kirocrew-worker
created: 2026-10-23
last-audited: 2026-10-28
audited-at: 33590d6f58
doc-pr: 23393
implementation-prs: [19040]
tracking-issues: [18998]
supersedes: []
superseded-by: []
---

# RFC: Remove a saved workflow from the library

- Status: in-progress. Nothing of it is on main. The implementation is
  [#19040](https://github.com/kirodotdev/KiroCrew/pull/19040), open, for
  [#18998](https://github.com/kirodotdev/KiroCrew/issues/18998). This document
  lands on its own first, because #19040 adds a capability that the implemented
  [rfc-global-workflow-library.md](rfc-global-workflow-library.md) lists as a
  non-goal, and the First Principles lane reads that decision off the base
  branch. Acceptance is requested from a maintainer: the maintainer who
  approves and merges this document records the decision.
- Amends [rfc-global-workflow-library.md](rfc-global-workflow-library.md) in
  this document, so that implemented document is not edited, in two places:
  - §4, the non-goal "Adding deletion, archival, import/export, or remote
    synchronization". Deletion leaves that list as the removal described here.
    Archival as a state a user can see and restore, import/export and remote
    synchronization stay non-goals.
  - §13 open question 3, "Should deletion be permanent, soft-delete, or
    archive-only". Section 4 answers it: a soft delete, kept as a tombstone,
    that Kiro Crew offers no undo for. It meets the amended document's §13 closing rule
    that any deletion design must preserve the meaning of lineage and completed
    run snapshots.

  Every other decision of that document stands, WFL1 to WFL13 included, with
  one clarification of WFL9 in section 8. Section 5 below adds WFL14 to WFL17
  to its §5 table.
- Claims about main were checked at `33590d6f58`, and about #19040 at its head
  `0444a4d64d`.

## 1. Problem

Once a workflow is saved to the library, nothing in Kiro Crew removes it. At
`33590d6f58`, `WorkflowDefinitionLibrary` in `src/kiro_crew/workflows/library.py`
has `create`, `update`, `get`, `list` and `search`, and no removal.
`/api/workflows/definitions/{workflow_ref}` answers GET and PATCH only
(`_register_mcp_routes` in `src/kiro_crew/dashboard/server_runtime/mcp_routes.py`).
A stale workflow keeps its row in Customize > Workflows, still runs from
`/workflow <slug>`, is still offered to agents by `workflow_library_list`, and,
when it is a Python workflow, still seeds intent authoring as a reference
example.

The only way out is to move its JSON file out of
`<KIROCREW_HOME>/workflow_library/` by hand. That breaks lineage, which the
amended document asks every deletion design to keep. `save_definition` in
`src/kiro_crew/workflows/service.py` checks a `derived_from` reference against
`list()`, and `promote_run_definition` saves through it. Once the parent's file
is gone, a draft or a completed run adapted from that parent is refused with
"workflow lineage does not resolve to a saved revision".

## 2. Goals

1. The owner can remove a saved workflow in Customize > Workflows, after a
   confirmation that names it.
2. A removed workflow can no longer be listed, searched or run, and it is no
   longer offered to agents or to intent authoring.
3. Everything that names a removed workflow keeps its meaning. Drafts and runs
   adapted from it still save, run snapshots keep the source they executed, and
   neither the id nor the slug it holds when it is removed ever reaches a
   different workflow.

## 3. Non-goals

- Restoring a removed workflow. The tombstone keeps every revision, so a later
  document can add restore without a migration.
- Removal from an agent tool, an MCP tool or the CLI.
- Removing one revision, or deleting the run history of a removed workflow.

## 4. Design

### 4.1 Removal is a tombstone

`WorkflowDefinitionLibrary.remove(ref)` sets `removed_at` on the record. No file
is deleted, and every revision stays on disk as stored. A v1 record also gains
the default `format` that every reader already assumes for it.

The tombstone rewrites the record as stored and runs neither the save-time
source gate nor redaction over it. A record saved under older redaction rules,
or placed in the folder by hand, therefore stays removable and keeps every
revision's source unchanged. It replaces the file the record was
read from, whatever that file is named. When more than one file holds a live
copy of the id, every copy is tombstoned, so no copy stays runnable after a
removal succeeds. Removals and revision updates take one cross-process lock,
`.revisions.lock`, across the read, the check and the replacement, so neither
undoes the other.

### 4.2 What leaves and what stays

| Reader | After removal |
|---|---|
| Customize > Workflows list and search | absent |
| `get` by id or slug, `/workflow <slug>`, `POST /api/workflows/definitions/{ref}/run`, `workflow_run` with `workflow` | not found, with no fallback to intent authoring (the amended document's §7) |
| `workflow_library_list` | absent |
| Intent authoring reference examples | absent |
| `PATCH /api/workflows/definitions/{ref}` | refused with 404, or with 409 when a removal in another process lands during the update; no update brings it back |
| The `derived_from` check in `save_definition`, for drafts and run promotion | still resolves the removed id and revision |
| The record and every revision | kept on disk |
| Run snapshots, runs of the removed workflow included | unchanged, each with the source it executed |
| Its id and its slug | reserved for good: no later record answers to either |

Lineage resolution reads `lineage_candidates()`, the one library read method
that returns a removed record. Lineage names a parent by id and revision, never by
slug, and is not a lookup by ref, so the reservation below never refuses it: a
removed parent still proves where a draft or a run came from.

The rules below take every id and slug to be a non-blank string, as in every
record the library writes. A file written by hand whose id or slug holds any
other value answers to nothing by that value, and this section does not
describe it further.

The id and the slug a workflow holds when it is removed are reserved for good.
Every later `/workflow <id>` or `/workflow <slug>` naming either answers not
found, and never runs a workflow saved afterwards, wherever that command was
written down. Every lookup by ref (`get`, `/workflow`, the run route,
`workflow_run` with `workflow`, and the PATCH and DELETE routes) refuses a
string that a removed record holds as its id or its slug, whichever live record
also carries that string, as its id or as its slug. Slug allocation never hands
one out either. It counts every id and every slug on disk, live or removed, as
taken, apart from a renamed record's own id and slug when no removed record and
no live record with another id holds them, so a new workflow or a rename whose
requested slug comes out as one of the taken ones gets the first free numbered
slug instead, `<slug>-2` when that is free.

The reservation covers only what the record holds at removal. A rename frees
the old slug at once, as it does today, so a slug given up before the removal
is not reserved; [#23680](https://github.com/kirodotdev/KiroCrew/issues/23680)
tracks that rename case on its own. The reservation also lives in the removed
record's file: deleting or damaging that file releases its id and slug, and the
library logs a file it cannot read at warning level.

One read of the library decides which record each ref reaches, and every
lookup and every list on that read uses the same answer. A ref that a removed
record holds reaches nothing. Any other ref reaches the first live record, in
the order the folder lists files, whose id it is, and only when no live record
has it as an id, the first whose slug it is. On main today, `get` returns the
first record in that order whose id or slug is the ref, so trying ids first is
a behaviour change (section 7). It changes an answer only when one record's
slug is another record's id, and only a record written or edited by hand puts a
library in that case: `create` mints every id as `wfd_` and 16 hex digits, and
the amended document's §6.2 limits a slug the library allocates to lowercase
letters, digits and hyphens. The reason for the order is that the dashboard
sends each save, run and removal to a record's id, the one ref a rename never
moves, and sends that id as `expected_id` with a save or a removal. If another
record answered to the id first, `/workflow <id>` would run that other
workflow, and every save or removal of this one would be refused as a
conflict.

Every list offers a record only under a ref of its own that reaches it: its
slug, or its id when the slug misses it. A record's slug or id misses it when a
removed record holds that string, or when another live record answers to it
first, one whose id is this record's slug or an earlier one with the same slug.
A record written into the library folder by hand, such as a copied or restored
file, can be in either case. Two files that hold copies of one id count as one
record here: a ref they share reaches the first in folder order, when it reaches
either. Customize > Workflows, `/workflow` with no name and
`workflow_library_list` list it under the ref that reaches it, so a listed
command always runs the record it is listed on. The editor sends its requests to
the id, or to the slug when the id misses the record. Every save and removal
from the editor sends `expected_id`, the id the owner saw, and when the ref
names a different workflow as the request is checked, the PATCH or DELETE route
answers 409 `workflow_definition_conflict` without changing anything, after
which the dashboard reads the list again. A revision saved from the dashboard
sends the slug field as shown, so a record whose slug misses it takes a new slug
that reaches it, from the slug allocation above, unless the owner clears the
field, which keeps the stored slug.

When both miss, the record has no runnable ref. Because ids are matched first,
only a removed record can make a live record's id miss it, as with a restored
copy or a copy that a failed removal left live (section 4.3). Customize >
Workflows keeps its row, with a "No slash command" label in place of a
`/workflow` command, its Run, Save new revision and Remove actions off, and a
line telling its author to give its file a new id and slug or to delete the
file. The chat `/workflow` list leaves it out, and so does
`workflow_library_list`, with or without a search.

### 4.3 Who removes, and the confirmation

Only the owner removes, and only in the dashboard.
`DELETE /api/workflows/definitions/{ref}` is the one entry point. It sits behind
the same two checks as the create and PATCH routes: a positively authenticated
dashboard user (an app token is refused) and `require_owner_dashboard_request`.

No model-facing MCP tool, agent tool or CLI command removes a definition. The
amended document keeps create and update off the model-facing surface for the
same reason (its §6.7): the core MCP server auto-approves its own handlers, so a
tool call cannot prove that a person chose the change.

The editor shows a **Remove from library** button, set apart from the save
button. The confirmation names the workflow and says, before anything is
removed:

- that Kiro Crew cannot undo the removal;
- that the workflow leaves the library and agents stop suggesting it;
- that the command the library lists it under stops working and stays
  reserved, so a new workflow cannot take that name and gets a numbered one
  instead. The line names that command as listed, `/workflow <slug>` with the
  real slug, or `/workflow <id>` when the slug misses the workflow;
- that past runs and workflows adapted from it keep working.

An unknown or already-removed ref answers 404 `workflow_definition_not_found`. A
failed write answers 500 `workflow_definition_write_failed`. The workflow then
stays in the library, the editor says so, and the owner can try again. The
exception is a write that fails after one of several copies of the record was
tombstoned. The id is then reserved, so a copy left live answers only to its
own slug, while that slug still reaches it, and otherwise has no runnable ref,
as section 4.2 describes.

### 4.4 The audit record

Each completed removal writes one SEL `api_access` row, with operation
`workflow_definition_remove`, outcome `completed`, the dashboard caller, and
resource `workflow_definition:<id>`. A failed write records outcome `failed`.
The route's allow and deny decisions are audited like those of the sibling
routes. Who removed a workflow lives in that row. The tombstone stores only
`removed_at`.

## 5. Invariants added to the amended document's §5

| ID | Invariant |
|---|---|
| WFL14 | Removal is a tombstone: the record keeps every revision on disk and gains `removed_at`, and no definition file is deleted |
| WFL15 | A removed definition is absent from every read that lists, searches, runs or offers definitions; only lineage resolution by id and revision returns it, and its id and slug still count as held wherever a ref is resolved or a slug is allocated or offered as a `/workflow` command |
| WFL16 | The id and the slug a definition holds when it is removed are never released while the removed record's file still reads as a removed record: no later record answers to either, by id or by slug, and slug allocation never hands either out; a slug renamed away before the removal is not covered (#23680). Any other ref reaches a live record by its id before any by its slug, and every list offers a live record only under a ref of its own that reaches it; one that neither reaches gets no `/workflow` command, and the dashboard lists it with its actions off |
| WFL17 | Only the owner removes, through the dashboard route, after a confirmation; no model-facing tool removes, and each completed removal writes one SEL row with outcome `completed` and resource `workflow_definition:<id>` |

## 6. Migration plan

| Phase | PR | Exit criteria |
|---|---|---|
| 1. Removal | #19040 | A removed record keeps every revision; it is absent from list, search, `get`, `/workflow` and authoring examples, and no saved-workflow run starts from it; drafts and runs adapted from it still save and promote; the id and slug it holds when removed stay reserved for every lookup by ref and for create and rename; any other ref reaches a record by its id before any by its slug, every list offers a record only under a ref that reaches it, and a live record that neither of its refs reaches keeps its dashboard row with its actions off and gets no `/workflow` command; the DELETE route is owner-only and writes one `completed` audit row per removal; the confirmation names the command the workflow is listed under |

## 7. Backward compatibility

Removal itself is additive. It is the only new write to the library, it touches
only the record being removed, and existing libraries need no migration.

Which record a ref reaches also changes for records that were never removed, so
the design is a behaviour change as well. The change shows only where one string
names two records, as section 4.2 describes:

- A lookup by ref tries ids before slugs. On main, when one record's slug is
  another record's id, the order the folder lists files decides which of the two
  `/workflow <that string>` runs. Under this design the record with that id
  runs, for the reason section 4.2 gives. Only a library holding a record
  written or edited by hand meets this case.
- Every list offers a record only under a ref that reaches it. On main,
  Customize > Workflows, `/workflow` with no name and `workflow_library_list`
  show every record as `/workflow <slug>`, even one whose slug reaches another
  record. Under this design that record is listed as `/workflow <id>`, so a
  listed command runs the record it is listed on.
- Slug allocation counts every id on disk as taken, as well as every slug. On
  main it counts slugs only, so a new or renamed workflow can take a string that
  a record written by hand holds as its id.

Every other record keeps answering every lookup by its id and by its slug, as
today, unless a removed record holds that id or slug (section 4.2).

## 8. Security considerations

Removal adds one owner-only mutation route and no model-facing surface. App
tokens are refused before the owner check, as on the sibling create and PATCH
routes. A removed workflow cannot run under its old id or slug, and no later
workflow can run under either while the removed record's file still reads as a
removed record (section 4.2).

A tombstone adds `removed_at`, and a v1 record's default `format`, to a record
already on disk, and writes no new source. It runs neither the save-time source
gate nor redaction over the stored record, because the gate would refuse, and
redaction would change, a record saved under older rules. This is the one
clarification of the amended document's WFL9: a tombstone is not a write of new
content. Redaction still runs at every egress boundary that document's §8.1
names, and no read that leaves the library returns a removed record.

## 9. Alternatives considered

- **Delete the definition file.** Rejected: `save_definition` then refuses every
  draft and run adapted from it, which breaks the lineage the amended document
  asks a deletion design to keep.
- **Release the slug, or the id.** Rejected: a `/workflow <slug>` or
  `/workflow <id>` written down before the removal would run whatever workflow
  takes that string next, as its slug or as its id.
- **Archive with restore.** Deferred: the tombstone keeps every revision, so
  restore can be added later without changing what removal stores.
- **A model-facing remove tool.** Rejected for the reason in section 4.3.

## 10. Open questions

None.
