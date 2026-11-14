# Ultra Mode Task Hierarchy Implementation Plan

> **Nothing implemented. On hold.** The owner paused this project on
> 2026-11-09; the plan is kept so the work can resume. `WorkItem` in
> `src/kiro_crew/work_ledger_records.py` has no `parent` field, `WORK_ACTIONS`
> in `src/kiro_crew/work_vocab.py` has no `dep` or `assign`, and
> `SCHEMA_VERSION` is `1`. Its spec is
> [`../rfc-ultra-mode-task-hierarchy.md`](../rfc-ultra-mode-task-hierarchy.md).

**Goal:** give Ultra Mode a task tree of any depth, dependency edges between
tasks, and many-to-many assignment of tasks to sessions, all in the work-ledger
core, with one shared conductor procedure on top.

**Scope:** every capability in the RFC ships. The implementation below is the
chosen one.

## Implementation principle: write facts, derive everything

The crew log is an append-only event log and the `work` fold is its projection.

1. **Only facts are written.** A task was created under a parent; an edge was
   added or removed; a session was assigned or released; a node was moved,
   split, merged, retitled or dropped; the goal changed. Each is one crew-log
   entry from `work_ledger_record`.
2. **Everything else is derived at read time and never stored**: the tree
   shape, roll-ups, the ready set, critical-path length, blocked-by-upstream,
   active-assignment counts. Nothing derived can drift from the facts, and
   there is nothing to migrate.
3. **Old entries are already valid facts.** An entry with no `parent` folds
   under the root; a `worker_session_key` folds as one owner assignment. Old
   data is not rewritten.
4. **Sub-ledgers are joined, not merged.** A child conductor's ledger keeps its
   own entries; the read side attaches it under the parent item it names
   (`parent_item`).
5. **One entry per user-visible action.** A split (new children plus moved
   edges) is a single crew-log line, so the fold never sees half a
   restructure. A split too large for the 64 KB line cap is refused, not
   truncated.

## Smallest tool surface

| Action | Change |
|---|---|
| `create` | gains optional `parent`, `needs: [ids]`, `milestone`, `priority` |
| `dep` (new) | `{add: [[from, to]], remove: [[from, to]]}`, cycle-checked against the fold |
| `assign` (new) | `{task, session, role, release?}`; `bind` stays as `assign role=owner` |
| `restructure` (new) | `{op: move \| split \| merge \| drop \| retitle, ...}`, one entry, with a reason |
| `goal` | rewording keeps counts; the root node is the goal |
| `work_ledger_read` | gains `view=tree` (nested, with roll-ups) and `view=ready` (ordered ready set) |
| `work_dispatch` | without an explicit item, takes the head of the ready set |

## Rulings

The RFC's open questions, decided by standard practice.

| # | Question | Ruling |
|---|---|---|
| Q1 | Tree shape cap | No hard cap. Soft patrol signal past about 7 children or about 6 levels |
| Q2 | Parent acceptance | Both required: all children accepted (or dropped) AND the parent's own bar |
| Q3 | Edges on split | Copied to every new node; the lead prunes |
| Q4 | Sub-conductor edges | Only inside its subtree; edges that leave it go through the parent lead |
| Q5 | Milestones source | Task-tree milestone nodes; the Goals tab reads them |
| Q6 | Affinity in ordering | Not in v1 |

## Tasks (7 PRs)

Each row is one PR with one acceptance bar: its checks green, then merged.
`Needs` lists the tasks that must merge first, so the plan is itself the
dependency graph the RFC describes.

| ID | PR | Needs |
|---|---|---|
| R0 | RFC and this plan in `docs/request-for-change/` | - |
| F1 | **Fold core.** Facts: `parent`, edges, assignments (entries and fold). Derived: tree, roll-ups, ready set, critical path, active-assignment count. Old entries fold unchanged. Sub-ledger join. Pure functions with unit tests on synthetic logs, including replay of real ledgers. | R0 |
| F2 | **Write API.** `create` fields, `dep`, `assign` (`bind` alias), cycle refusal; `concurrency_cap` counts active assignments, `item_cap` counts leaves; `work_report` takes `task_id` | F1 |
| F3 | **Read and dispatch.** `work_ledger_read view=tree / view=ready`; `work_dispatch` from the ready set; `blocked_by_upstream` on rejected or abandoned predecessors | F2 |
| F4 | **Restructure and goal.** `restructure` ops as single entries; the goal is the root node; `goal` rewording keeps counts | F2 |
| U1 | **Board.** goal-board Status as a collapsible tree with roll-ups, milestones and "waiting on X"; Crew tab rows list held tasks; Goals-tab milestones read tree nodes | F3 |
| S1 | **Skills.** New shared `task-graph` skill; goal-conductor's Independent rule becomes edges; team-lead, pipeline (`parked` becomes edges) and security (chain template) keep their domain parts; one pointer in `spec_prompts.py`; remove `goal-ledger-conductor` | F3, F4 |

F3 and F4 run in parallel after F2. U1 and S1 run in parallel after F3 (S1 also
needs F4). Critical path: R0 -> F1 -> F2 -> F3 -> S1.

## Risks and how they are contained

| Risk | Containment |
|---|---|
| Fold cost on large trees | Derived views memoized per fold generation; recomputed only when a new entry lands |
| A restructure that orphans edges or assignments | The entry is validated against the current fold before it is written; refused with the conflicting ids |
| Old readers (goal-board v1, existing conductors) | `items` keeps its current shape; tree and ready are new fields beside it |
| Two writers on one ledger | Unchanged: one conductor owns its ledger; sub-conductors write only their own |

## Shared-file owners

| File | Order |
|---|---|
| `src/kiro_crew/work_ledger_records.py` and the `work` fold | F1 -> F2 -> F4 |
| `src/kiro_crew/dashboard/handlers/work_ledger_conductor.py` | F2 -> F3 -> F4 |
| `src/kiro_crew/dashboard_templates/builtin/goal-board/` | U1 |
| conductor skills and `spec_prompts.py` | S1 |

Each PR's tests and review go to a different session than its author.
