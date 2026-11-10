---
title: Ultra Mode task hierarchy redesign
status: draft
author: Mingwei Chen (chenmingwei23)
created: 2026-11-08
last-audited: 2026-11-08
audited-at: 8ad14ca919
doc-pr: null
implementation-prs: []
tracking-issues: [34239]
supersedes: []
superseded-by: []
---

# RFC: Ultra Mode task hierarchy redesign

Status: draft, on hold (paused by the owner on 2026-11-09; kept so the work
can resume). Tier: T3. Nothing built; this document changes no code. Every
code reference was read at `8ad14ca919`. The task-by-task plan is
[`plans/2026-11-08-ultra-mode-task-hierarchy.md`](plans/2026-11-08-ultra-mode-task-hierarchy.md).
Scope: the work ledger, the Ultra Mode Crew tab and goal-board, and every
conductor-style skill and system prompt in this repository.

## Summary

Ultra Mode's task model squashes three different relations into one, "one task
= one worker session":

- **containment**: which larger piece this belongs to;
- **ordering**: what it must wait for;
- **execution**: who is doing it.

The bigger and longer the project, the more that distorts. Engineering
management has kept the three apart for sixty years (a work breakdown structure
plus a critical-path network; Jira's hierarchy plus issue links), and so should
we.

**The problem it solves.** On long projects, the tasks under a goal pile up as
one flat list, and users cannot see phases, blockers or what is left. Ordering
is forbidden by rule and lives only in the lead's memory, so workers get
dispatched to wait, or in the wrong order. Adding a level costs a whole extra
conductor session, so nobody does it. And each conductor writes its own
decomposition rules, which drift apart.

**What changes.**

1. Tasks get parents and form a tree of any depth that can be reshaped while
   work runs. The goal is the root and can change too.
2. Tasks can depend on each other (a DAG). Dispatch takes only from the ready
   set (all predecessors accepted), critical path first.
3. Tasks and sessions are decoupled into a many-to-many assignment. The
   concurrency cap counts working assignments, not open tasks.
4. The model goes into the work-ledger core. Every conductor shares one
   procedure, and each skill keeps only its domain part.

## 1. Why

### 1.1 What users see

Ultra Mode presents three concepts: **goal**, **milestone** and **task**. Users
expect them to nest. The data has only two levels: one goal and a flat list of
work items. The Milestones section of the Ultra Mode chat's Crew tab draws one
row per work-ledger item
(`website/src/pages/chat/command-center/UltraCrewPanel.tsx`), and the goal-board
page reads the same list from its `items` field
(`src/kiro_crew/dashboard_templates/builtin/goal-board/manifest.json`). Every
entry in that list is at once a task and a worker.

The longer a project runs, the longer that flat list gets. Users cannot tell
which entries belong to the same phase, which entry waits on which, or how far
the goal is from done. The first thing lost is the sense of "how much is left",
which is the one thing the user needs most.

### 1.2 Root cause: what is wrong in the architecture

This is not a presentation problem. The data model is missing some things and
binds together others that should be separate.

| # | Problem | Where in the code | Consequence |
|---|---|---|---|
| 1 | **Tasks have no parent.** `WorkItem` has `title`, `acceptance`, `worker_session_key` and the report fields, but no `parent`. | `src/kiro_crew/work_ledger_records.py` `class WorkItem` | "Phase -> task" cannot be expressed. The only way to add a level is to open another conductor session with its own ledger (`ConductorRecord.parent_item`): **the session tree is used to fake a task tree**. |
| 2 | **Task and session are bound one-to-one.** A session holds one open item at a time; a second bind is refused `worker_already_dispatched`. | `src/kiro_crew/dashboard/handlers/work_ledger_conductor.py` | Two common shapes cannot be written down: one worker doing several small things, and several workers on one large thing. Tasks get cut to exactly one worker's size, so granularity follows staffing, not the work. |
| 3 | **Depth is tied to sessions.** `MAX_DEPTH = WORK_MAX_DEPTH_DEFAULT` is 2 (the root and one conducting level; `work_ledger.max_depth` raises it to at most 10). A conductor past it is refused `depth_exceeded`, and team-lead tells the lead that the refusal "is the signal to flatten that branch into workers". | `src/kiro_crew/work_ledger_records.py`, `src/kiro_crew/work_vocab.py`, `builtin_skills/team-lead/SKILL.md` section 4 | One more level of tasks costs one more level of conductor sessions: a whole session plus a patrol loop. In practice people avoid levels, so the list only grows flatter and longer. |
| 4 | **Dependencies are explicitly forbidden.** goal-conductor's first qualification rule is *Independent*: "Two candidates that hand off to each other are one sequence inside a single item." | `builtin_skills/goal-conductor/SKILL.md` "Work-item qualification" | Real work is full of ordering. The rule can only hide it: either fold it into one big item and lose parallelism, or keep "dispatch B after A" in the lead's head, where it is lost on a lead change or a context compaction. |
| 5 | **Limits count the flat list.** `DEFAULT_GOAL_ITEM_CAP = 20` (advisory) and `DEFAULT_GOAL_CONCURRENCY_CAP = 6` (enforced, counts open items). The crew-log fold forgets a closed item once `WORK_CLOSED_WINDOW = 32` newer closed items sit behind it; the goal-board shows the rest only as `omitted` / `forgotten` counts. | `src/kiro_crew/work_ledger_records.py`, `src/kiro_crew/work_vocab.py`, goal-board `forgotten` field | A long project's history collapses into a number. The concurrency cap counts open items, not working workers, so a task waiting on a prerequisite still takes a slot. |
| 6 | **The goal is a string, not a node.** It can only be reworded with a `goal` write, or replaced with `new_goal`. | `ConductorRecord.goal` / `goal_version` | When the objective changes, the tasks under it do not reorganise; a new goal restarts the goal's item count and breaks the history. |
| 7 | **Every conductor reinvents it.** See the table in section 6. | `src/kiro_crew/builtin_skills/*`, `src/kiro_crew/agent_parts/spec_prompts.py` | The same procedure (decompose, order, dispatch, accept) is written in four skills and several system-prompt blocks, and the copies drift. pipeline-conductor even built its own `parked` list in `conductor-status/v1` ("each with the dependency that parked it") to patch the missing dependency concept. |

**Core diagnosis: three relations are squashed into one.**

- **Containment**: which larger piece this is part of. Used to roll up progress
  and to talk about scope.
- **Ordering**: which piece this must wait for. Used to decide when it can be
  dispatched.
- **Execution**: who is doing it. Used to manage capacity and patrol.

Only execution is first-class today (the session tree). Containment borrows the
session tree, and ordering is ruled out. The longer the project, the further
these three diverge, and the worse it gets to keep them in one structure.

## 2. Why project management is hierarchical: the historical answer

Engineering management answered this long before Jira.

### 2.1 Hierarchy is a limit of human attention

- **People hold few things at once.** After Miller (1956), "seven plus or minus
  two", later work puts the number lower, around four chunks. A flat list past
  a dozen entries can only be scanned line by line, never seen as a whole.
  Hierarchy is *chunking*: each level shows only a few children, and the reader
  understands the whole by expanding level by level.
- **Different readers read different levels.** The doer reads leaves, the team
  lead reads phases, the requester reads only the top. A flat list forces
  everyone to read everything.
- **Progress has to roll up.** "This phase is 70% done" is computable only when
  the phase is a node with children. A flat list answers "how many closed",
  which says little about distance to the goal.
- **Change has to stay local.** A changed objective usually affects one branch.
  With a hierarchy you replace the branch; with a flat list you hunt entry by
  entry.

### 2.2 Work Breakdown Structure and the critical path

In the 1950s and 60s, US defence and space programmes (Polaris among them)
became too large for a single schedule, and produced two methods that are still
foundational:

- **WBS (Work Breakdown Structure)**: decompose deliverables by containment,
  level by level, until every leaf can be estimated and accepted on its own. It
  was later codified in a military standard (MIL-STD-881). Its core rule is the
  *100% rule*: the children add up to exactly the parent, no more and no less.
- **PERT / CPM**: draw ordering edges between leaves to form a directed acyclic
  graph, and find the **critical path**, the longest dependency chain. It sets
  the shortest possible completion time and tells you where to put resources
  first.

The two were always separate: **the WBS is a tree that governs scope; the
network is a DAG that governs time.** The same leaf appears in both. That is
exactly the structure we are missing.

### 2.3 Why Jira did not stay flat

Jira started as a flat issue tracker: each issue a row, organised by fields and
filters. It added hierarchy step by step, each step forced by its users' scale:

1. **Sub-tasks**: an issue too big for one person, and a flat list cannot say
   "these rows are one thing".
2. **Epics**: arrived with agile workflows (Scrum / Kanban boards). Once the
   backlog grew, teams needed a container larger than a story to prioritise and
   report on.
3. **Levels above Epic** (Initiative and so on, configurable): multi-team,
   multi-quarter planning needed another roll-up.
4. **Issue links (blocks / is blocked by)**: a second relation beside the
   hierarchy, expressing ordering. It was not made part of the hierarchy,
   because dependencies cross branches.

Two lessons. First, **flat only works at small scale**; containment becomes
necessary as scale grows. Second, **containment and dependency have always been
two distinct relations**; Jira never tried to express one with the other. We
currently lack both, and prop up the first with the session tree.

### 2.4 But the number of levels should not be fixed

Jira also teaches the opposite lesson: its levels are fixed (Epic / Story /
Sub-task), and teams keep finding work "bigger than a story, smaller than an
epic" and forcing it into one. Our case is more extreme: an Ultra Mode goal is
often unclear at the start, and **its structure grows while the work runs**. So
this design does not define epic / story / task levels. It defines a tree of any
depth that can be reshaped at any time. Level names are display labels only and
do not enter the data model.

## 3. Design: three structures, one job each

```
Task tree (containment) + dependency edges (ordering)     Session tree (execution)

                 goal (mutable)                                team lead
                /              \                              /         \
          Phase A              Phase B                  worker 1      conductor
          /     \              /     \                                    |
     A1 [done]  A2 [ready]  B1 [ready]  B2 [waits]                    worker 2
                                        /     \
                                      B2a     B2b

Dependency edges (may cross branches):  A1 -> A2,  A2 -> B2a,  B1 -> B2b
Assignments (many-to-many):             worker 1 holds A2,  worker 2 holds B1
```

### 3.1 Task tree (containment)

- **A node** is today's work item plus a `parent` field. The root is the goal,
  so the goal is a node and can be reworded or split like any other.
- **Depth is not fixed**, and is independent of session depth;
  `work_ledger.max_depth` no longer limits it. A loose structural cap (say 8
  levels) guards against runaway trees, and it limits only the tree's shape,
  never sessions.
- **Acceptance can sit at any level.** Leaves use today's `acceptance` kinds
  (`pr_checks`, `pr_merged`, `file`, `human_approval`, `edition_check`). A
  parent defaults to "all children accepted" and may add its own bar (for
  example a phase-level `human_approval`).
- **State rolls up automatically.** A parent's progress, needs-you count and
  blocked count are computed from its children, not hand-written by the lead.
  The Crew tab's List view already does this for session subtrees: a folded
  lead shows its child count, how many below it wait on an approval and how
  many run ([#30432](https://github.com/kirodotdev/KiroCrew/pull/30432)). The
  task tree gets the same.
- **Milestone stops being a separate concept.** Any node can be flagged as a
  milestone; the Crew tab's Milestones section reads flagged nodes, not the
  whole list. Milestones on a Bot's Goals tab (set up through
  `src/kiro_crew/dashboard/goal_setup.py`) map onto the same kind of node, so
  the two finally agree.

### 3.2 Dynamic restructuring: the tree grows as the work runs

It can start with one level: a goal with a few tasks under it. As work proceeds,
the lead reshapes it with the operations below. Each one is an event with a
reason, so restructuring has its own history.

| Operation | Meaning | Invariant |
|---|---|---|
| `add_child` | Add a child under a node | A closed parent reopens |
| `split` | Split a node into siblings, or into a new level of children | Dependency edges move to the new nodes by "who needs what"; assignments move or are released |
| `merge` | Merge several nodes into one | Dependencies are the union, minus internal edges |
| `move` | Change a node's parent | Never into its own subtree (no cycles) |
| `retitle` | Change title and description, including the goal | Bumps `version`; changing the goal does not reset counts |
| `drop` | Abandon a node and its subtree | State `abandoned`; dependents are flagged for the lead to decide |

When to restructure is written into the shared procedure (section 6) as patrol
signals: a node has more than about 7 children; a leaf fails acceptance twice in
a row; a worker's report surfaces an unplanned sub-problem; the user changes the
objective.

### 3.3 Dependency graph (DAG)

- **An edge** `A -> B` means "B cannot start until A is accepted". Edges may join
  any two nodes, across branches and to parents ("wait for all of phase A").
- **Acyclic**: every edge insert is cycle-checked; a cycle is refused and the
  nodes on it are reported.
- **Containment and dependency do not imply each other**: a parent does not
  depend on its children (roll-up already covers that); depending on a parent
  means depending on its whole subtree.
- **Hand-off can be recorded**: an edge may carry what A hands to B (branch,
  file path, PR number). Today that lives only in seed text and is lost when the
  lead compacts.

### 3.4 Dispatch: only ready work, critical path first

With a dependency graph, dispatch moves from "the lead remembers" to "take from
the ready set":

```
ready = { leaf n | n is open
                  and every predecessor of n is accepted
                  and n has no active assignment }

sort key = (user priority, remaining critical-path length desc, created_at)
each round: dispatch the top k of ready, k = concurrency cap - active assignments
```

That is what "take the load off workers" means here:

- **No worker sits waiting.** A task whose prerequisites are not done is never
  dispatched, so no session is open waiting on upstream and holding a slot.
- **Unblock others first.** Critical-path tasks go first, because they have the
  most work queued behind them.
- **The cap counts active assignments**, not open nodes. The tree can be large
  while the number of sessions in use stays bounded. This fixes problem 5 in
  section 1.2: queued tasks no longer take slots.
- **Upstream failure is surfaced.** When a node is rejected or abandoned, its
  direct dependents are marked `blocked_by_upstream` and enter the lead's next
  patrol instead of stalling silently.

### 3.5 Assignment: tasks and sessions are many-to-many

- **An assignment** is its own record: `(task_id, session_key, role, since,
  until)`, with `role` one of `owner` / `helper` / `reviewer`.
- One session may hold several small tasks (for example three changes in the
  same worktree); one task may have an owner plus helpers and reviewers. The
  existing team-lead rule "Testing and review are their own item, dispatched to
  a different child than the author" (team-lead section 5) becomes data: the
  author is `owner`, the reviewer is `reviewer`.
- **Reports target tasks, not sessions.** `work_report` carries a `task_id`; it
  may be omitted when the session holds one task, which keeps today's usage
  working.
- A task has at most one `owner` at a time. This is the one constraint kept, so
  "who lands this" is always clear (team-lead section 5, "one integrator per
  output").

### 3.6 The session tree (execution) stays as it is

The session tree keeps answering "who is running, who dispatched whom, who
should be closed", and the Crew tab's List view does not change. The one change:
**a sub-conductor is no longer opened to add a level**, only when a branch truly
needs someone to keep conducting it. A sub-conductor receives a subtree of the
task tree (and may restructure inside it), not a new ledger. There are then no
separate ledgers to stitch together, and no need for a dedicated tracker worker
(team-lead section 5, "Add a tracker when one patrol read no longer fits") to
summarise several ledgers.

## 4. Data model changes

```
# Node (WorkItem extension)
parent:        str | None        # None = root (goal)
kind:          "goal" | "node"   # only the root is a goal
milestone:     bool              # shown under Milestones
order:         int               # display order among siblings
priority:      int | None        # user-set priority
acceptance:    {...} | None      # required on leaves; optional on parents, default all_children_accepted
version:       int               # bumped by retitle / split / merge

# Dependency edge (new)
DepEdge(from_id, to_id, handoff: str = "", created_at, removed_at)

# Assignment (new; replaces the one-to-one WorkItem.worker_session_key)
Assignment(task_id, session_key, role, since, until)

# Events (WORK_EVENT_KINDS gains kinds)
"restructure" | "dep_add" | "dep_remove" | "assign" | "unassign" | "goal_change"
```

**Principle: write facts, derive everything.** The crew log is an append-only
event log and the `work` fold is its projection. Only facts are written: a node
was created under a parent, an edge was added or removed, a session was
assigned or released, a node was restructured, the goal changed. The tree
shape, roll-ups, the ready set, the critical path and active-assignment counts
are derived at read time and never stored, so nothing derived can drift and
nothing needs migrating. Each user-visible action is one crew-log entry, so the
fold never sees half a restructure.

Tools, kept to the smallest surface:

| Action | Change |
|---|---|
| `create` | gains optional `parent`, `needs: [ids]`, `milestone`, `priority` |
| `dep` (new) | `{add: [[from, to]], remove: [[from, to]]}`, cycle-checked against the fold |
| `assign` (new) | `{task, session, role, release?}`; `bind` stays as `assign role=owner` |
| `restructure` (new) | `{op: move \| split \| merge \| drop \| retitle, ...}`, one entry, with a reason |
| `goal` | rewording keeps counts; the root node is the goal |
| `work_ledger_read` | gains `view=tree` (nested, with roll-ups) and `view=ready` (ordered ready set) |
| `work_dispatch` | without an explicit item, takes the head of the ready set |

## 5. Presentation

- **Crew tab Status view and goal-board**: the Milestones list turns from a flat
  list into a collapsible task tree; each node shows rolled-up progress and a
  needs-you count. Milestones shows flagged nodes only, counted as "accepted /
  total".
- **Dependencies visible**: a blocked node shows "waiting on X", linking to X.
  No full graph drawing; each node shows only its direct predecessors, to avoid
  a wall of lines.
- **The Crew tab's List view is unchanged**, except each row also shows which
  tasks it holds.
- **History is no longer folded away**: closed nodes stay in the tree (collapsed
  by default) instead of becoming a `forgotten` count.

## 6. Consolidation: one procedure for every conductor

The repository has five conductor-style skills, plus several system-prompt
blocks in `src/kiro_crew/agent_parts/spec_prompts.py`, each defining for itself
what a task is, how to order work, how to dispatch and how to accept. Line
counts are `SKILL.md` at `8ad14ca919`.

| Agent / skill | Lines | Its own task rules | Its own patch for dependencies |
|---|---|---|---|
| `goal-conductor` | 982 | Three qualification rules: Independent / Assertable / Boundary | None; explicitly forbidden |
| `team-lead` | 216 | The do-it-yourself test (section 3); dispatch a conductor by default, only one level deep (section 4) | None; held in the lead's memory |
| `pipeline-conductor` | 1666 | Two columns, issue / PR; its own `conductor-status/v1` | `parked`: records the dependency that parked each entry |
| `security-conductor` | 516 | "Independently auditable"; two human gates (`human_approval`) | Audit -> verify -> fix, carried in its seed templates and gates |
| `goal-ledger-conductor` | 15 | Deprecated alias of goal-conductor | - |
| `spec_prompts.py` | - | Decomposition rules inside `_CONDUCTOR_WORK_LEDGER_PROTOCOL`, `_TEAM_LEAD_SYSTEM_PROMPT`, `_PIPELINE_CONDUCTOR_SYSTEM_PROMPT`, `_SECURITY_CONDUCTOR_SYSTEM_PROMPT` | - |

**Proposal:**

1. **One shared procedure skill** (working name `task-graph`): when to build and
   reshape the tree, how to add dependencies, ready-set dispatch, roll-up and
   acceptance, patrol signals. This is the only place those rules are written.
2. **Each conductor skill keeps only its domain part:**
   - pipeline: the issue / PR columns become a fixed tree shape (issue node ->
     PR child); `parked` becomes dependency edges.
   - security: audit -> verify -> fix becomes a fixed dependency-chain template;
     the two human gates become `human_approval` nodes on that chain.
   - team-lead: "do it yourself or dispatch" stays, because it is about
     execution, not task structure.
   - goal-conductor: what remains is essentially the shared procedure and can
     become the body of `task-graph`.
3. **Rewrite goal-conductor's Independent rule** from "items must not hand off to
   each other" to "a hand-off is an edge". Assertable and Boundary stay; they
   constrain leaves and still hold.
4. **Remove `goal-ledger-conductor`**, whose own description already says it is
   "removed next release".
5. **Leave one pointer in each system prompt** to `task-graph`, and delete the
   duplicated decomposition rules, so the copies stop drifting. In team-lead's
   own words: "a procedure stated twice is a procedure that drifts, and the
   reader ends up following neither copy".

## 7. Compatibility and migration

- **Existing ledgers**: on read, flat items become direct children of the root;
  `worker_session_key` becomes one `owner` assignment; no dependency edges. Old
  records on disk are not rewritten; new fields appear only on new writes
  (`SCHEMA_VERSION` bump, today `1`).
- **Sub-conductor ledgers** (`ConductorRecord.parent_item`): on read, attached
  under the matching node of the parent ledger, forming one tree. New
  sub-conductors stop opening ledgers and take a subtree instead.
- **Existing tool calls**: `create / bind / decide / verdict / close / accept`
  keep working; `create` without `parent` attaches to the root.
- **Limits**: `concurrency_cap` counts active assignments; `item_cap` counts
  leaves, with a higher default since nodes no longer equal sessions.

## 8. Non-goals

- **Jira / external tracker integration**: not in this RFC. Nodes reserve an
  `external_ref` field; a separate RFC follows.
- **Effort estimates and Gantt charts**: the critical path uses remaining node
  count, not time.
- **Fixed level names** (epic / story / task): display labels only, not in the
  model.
- **Automatic restructuring**: the lead decides; the system only raises signals
  and never reshapes the tree itself.

## 9. Rulings

The open questions, decided by standard practice. The plan builds on these.

| # | Question | Ruling |
|---|---|---|
| Q1 | Structural cap: how many levels and children, refused or warned? | No hard cap. A soft patrol signal past about 7 children or about 6 levels |
| Q2 | Does a parent's own acceptance combine with "all children accepted", or override it? | Both are required: all children accepted (or dropped) AND the parent's own bar |
| Q3 | On `split`, how do dependency edges move? | Copied to every new node by default; the lead prunes |
| Q4 | May a sub-conductor change edges that leave its subtree? | No. It edits edges inside its subtree; edges that leave it go through the parent lead |
| Q5 | Goals-tab milestones vs task-tree milestones: which is authoritative? | Task-tree milestone nodes; the Goals tab reads them |
| Q6 | Should ready-set ordering consider affinity (same worktree / files)? | Not in v1

## 10. Phased rollout

Each phase is split into PR-sized tasks with their dependencies in the
[implementation plan](plans/2026-11-08-ultra-mode-task-hierarchy.md).

| Phase | Content | Done when |
|---|---|---|
| P1 Data model | `parent`, dependency edges, assignment records, compatible read of old ledgers, cycle detection | Old ledgers read the same as today; new tools build trees, add edges and refuse cycles |
| P2 Dispatch | Ready set, sort key, `work_dispatch` takes from the ready set; cap counts active assignments | A task with dependencies is never dispatched before its predecessors are accepted |
| P3 Presentation | Crew tab and goal-board task tree with roll-ups and dependency hints; milestone flag | On a long project, phase progress and blockers are visible at a glance |
| P4 Shared skill | `task-graph` procedure; the four conductors keep only their domain parts; remove `goal-ledger-conductor` | Decomposition and dispatch rules appear in exactly one place |
| P5 Restructuring | `split / merge / move / drop / goal_change` and patrol signals | After a goal change, the tree can be reshaped branch by branch with history kept |

## 11. Security considerations

No new trust boundary. Every new action (`add_child`, `split`, `dep_add`,
`assign` and the rest) is a conductor-side `work_ledger_record` write, behind
the same caller checks the existing actions pass. Workers still write only
their own report fields, now addressed by `task_id`. The structural cap and the
cycle check on every edge insert bound what a confused or hostile conductor can
build.

## 12. Alternatives considered

- **Keep the flat list and nest conductors** (today). Each level costs a
  session and a patrol loop, and the default cap refuses past the second level
  (section 1.2, problem 3).
- **Fixed levels** (epic / story / task). Rejected in section 2.4: Ultra Mode
  goals grow their structure while they run.
- **Dependencies as skill text only** (today's `parked` list and seed text).
  They are lost when the lead's context is compacted, and each conductor
  re-implements them (section 6).

References: Miller, G. A. (1956), "The Magical Number Seven, Plus or Minus Two";
US DoD MIL-STD-881 (Work Breakdown Structure); PERT (1958) and CPM (1957);
Atlassian Jira issue hierarchy and issue links.
