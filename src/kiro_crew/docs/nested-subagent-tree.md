# Nested Subagent Tree — Design

How a subagent spawned *by another subagent* gets its true parent, what actually
bounds nesting, and how the dashboard renders the chain of command as a live
indented tree.

- **Backend:** `src/kiro_crew/subagent.py`, `src/kiro_crew/session_tree.py`, `src/kiro_crew/mcp_core.py`
- **Frontend:** `website/src/pages/chat/SubagentProgressBar.tsx`, `SubagentRunCard.tsx`, `website/src/store/chatSlice.ts`
- **Related:** [Subagents](subagents.md), [Dynamic Sub-Agent Sizing](dynamic-subagent-sizing.md)

## Summary

`spawn_run` lets a subagent spawn its own children, forming an orchestration tree
(coordinator → team-lead → workers). Two things make that hard, and this doc
records both:

1. **Recovering the parent.** kiro-cli multiplexes subagent sessions onto one
   shared runtime and does not thread the originating session id through
   `tools/call`, so at spawn time the backend cannot tell *which* sibling is
   spawning. The tree is rebuilt from each node's own event stream instead.
2. **Bounding the tree.** Because the parent edge is unknowable at spawn time, a
   *depth* ceiling is structurally fail-open. Nesting is bounded by a **rooted
   node count** instead, which stays exact under the same flattening.

Nesting is opt-in (`agent.subagent_tree_attribution`, default `false`). The node
cap is enforced unconditionally.

## Part 1 — Attribution: recovering the true parent

### Root cause: caller identity is flattened

When a subagent calls `spawn_run`, the request reaches `/api/spawn` with a
`parent_session_key` derived from the MCP caller identity. On a shared runtime
sessions A / B / C all present the **same** identity (the runtime owner), so
that key cannot name the spawning sibling and every nested child would register
as a depth-1 sibling of its own parent.

Two ways of recovering identity from that flattened signal fail by construction,
and neither is used:

- **`session_pid` `/proc` walk** — write `session_pid_<runtime_pid> =
  subagent:<id>` and have the MCP child climb ancestors to find it. All siblings
  share one pid, so they clobber one file and everyone resolves to the last
  writer.
- **`_active_turns` stack** — have `/api/spawn` read the running-turn stack top
  as the parent. With N sibling turns live on one runtime the top is a guess, and
  it misattributes children across siblings.

### Fix: build the tree from each node's own stream

The parent → child edge does not need the caller identity at all — it is already
observable in the *spawner's* stream. `_run_inner(A)` streams exactly A's turn,
so when a `spawn_run` result event names child ids, the parent is unambiguously
`A`. Wiring, in the stream loop:

1. On `EVENT_TOOL_CALL`, record the `tool_call_id` in `_canonical_spawn_calls`
   **only** when `event.tool_name == "spawn_run"` *and* `event.mcp_server_name`
   is the core MCP server.
2. On `EVENT_TOOL_RESULT`, attribute only if that `tool_call_id` is in the set.

The gate is the canonical MCP envelope captured at call time — never the tool
name carried on the result, which is a model-influenced display title. A model
that titles some other tool `spawn_run` therefore cannot drive attribution.

Invariants that matter in the real implementation:

- **Anchored regex** (`_SPAWN_RESULT_ID_RE`) — a bare hex scan would match ids
  quoted in model prose, letting a parent claim a child it never spawned.
- **`_pending_attribution` exactly-once gating** — prevents one parent stealing
  another's child on overlapping streams.
- **Monotonic depth** — `max(child.depth, parent.depth + 1)`, never downward.

### Attribution is repair, not enforcement

Attribution corrects the tree edge and the depth. It **cancels nothing**. Depth
is observability: it drives indentation and the "N levels" badge, and nothing
gates on it. This is the main departure from the original design, which used
attribution to cancel over-depth children after the fact; see Part 2 for why
that role moved.

Because depth is now purely a UI concern, attribution is gated on
`agent.subagent_tree_attribution` — a UI feature behind a UI flag. The cap that
bounds nesting does not consult that flag.

### Delivery route vs tree edge

Two different questions are asked of a child's parent, and one field cannot
answer both:

| Field | Meaning | Must be |
|---|---|---|
| `parent_session_key` | where the child's completion is **delivered** | a real surface (`dashboard:<slot>`, a cron, …) |
| `tree_parent_key` | the true **tree edge** | may be `subagent:<id>` |

A `subagent:<id>` value names no deliverable surface, so a child carrying one as
its route has its result dropped. `_routable_parent_key` normalises at spawn:
resolve the child's own tree root and, if that root is a real surface, route
there while preserving the true edge in `tree_parent_key`.

Normalising at spawn rather than during attribution is deliberate — attribution
exists to repair flattening, so it does not run on the path where this bites
(`session_sharing = false`, where the nested spawn already arrives with its real
`subagent:` parent). Queued spawns are created *before* tree registration, so
they normalise again when drained.

## Part 2 — What bounds nesting: a rooted node count

### Why a depth ceiling is fail-open here

A depth ceiling needs the **precise parent edge** to compute a child's depth. On
a shared runtime that edge is exactly what flattening destroys, so at spawn time
every nested child computes as depth 1 and the ceiling never fires. Correcting
it afterwards means the child has already been admitted — the ceiling becomes
best-effort, which is not a ceiling.

### Why a node count is fail-closed

A node count needs only the **root**, and flattening collapses the parent *to*
the root. `count_for_session` resolves the root via `SessionTree.root_of` and
returns `subtree_size(root, include_self=False)` — a count of nodes, not a path
length. So the *structure* may be recorded wrongly while the *cardinality* stays
exact. That asymmetry is the whole reason the cap is a count.

### The cap

`agent.subagent_max_per_session` (UI: *SubAgent Max Tree Nodes*) bounds one
orchestration tree:

- Counts **every nesting level** under the root, excluding the root itself.
- **Plus anything queued** (`_queued_depth`, also resolved to the root). The
  global `max_subagents` cap only throttles what is *running*; ignoring the
  queue would make the queue the unbounded surface.
- Comparison is `>=`, and breaching it **refuses** the spawn with an error — it
  does not queue it.
- `0` is the **auto** sentinel: use the effective global cap. It never resolves
  to 0 — there is deliberately no unlimited setting — and it fails **closed** to
  the legacy floor (`3`) when the config cannot be read.

### Gate order matters

Inside `spawn()` the node cap is checked **before** the global slot/stagger
check. That ordering is load-bearing: over budget yields a clean refusal the
caller can act on, whereas the later gate parks the spawn in a queue and makes
it wait.

## Part 3 — Tree bookkeeping invariants

### Per-agent teardown must reparent, not prune

Every per-agent teardown path (`_run`'s completion, `_force_reap`) uses
`SessionTree.remove_and_reparent`. `prune_subtree` is correct **only** where the
node provably never ran — a spawn rejected by the cap, or a denied approval.

The reason is that neither `cancel()` nor the reaper cascades to descendants, so
a finishing or reaped parent can still have live children. Pruning removes those
children from the tree, which silently frees their budget under the node cap —
the cap under-counts and stops binding. A source-level contract test asserts
that no per-agent teardown path mentions `prune_subtree`.

## Part 4 — Why nesting uses `spawn_run`, not `spawn_sub_agents`

Nesting is specified on `spawn_run`, which is **non-blocking**: the parent
finishes its turn, releases its concurrency slot, and its children run on their
own slots with results routed to the tree root. Parents do not accumulate slots,
so nesting does not change concurrency accounting.

`spawn_sub_agents` is the blocking variant — it polls until every child settles,
up to `KIROCREW_SPAWN_SUB_AGENTS_MAX_WAIT` (default 7200 s), and the caller holds
its slot for the whole wait. Had nesting been built on it, a set of parents could
occupy every slot while their children sat in the queue, and `_drain_queue`
returns immediately while `_running_count >= _max_concurrent` — with no timer
that breaks the cycle, since the reschedule branch only fires when a slot is
already free. Nothing would progress until the waiters timed out.

Two properties are worth recording:

- It takes **at least two** trees. A single tree that owns every slot has a node
  count at its cap, so its next child is refused rather than queued; queueing
  requires the tree to be under its own budget while the shared slots are gone.
- This is a **liveness** property of the blocking tool, not something nesting
  introduces: the blocking spawn, the slot-gated drain, and the slot-holding
  caller all exist independently of the tree. Blast radius is the caller's own
  session — no privilege boundary is crossed.

The requirement is currently expressed in the system prefix (`_MAY_SPAWN_CLAUSE`
names `spawn_run` and warns off the blocking variant). That is a prompt-level
instruction, not a hard gate: the blocking tool remains callable. See Known gaps.

## Part 5 — Frontend: rendering the tree

### Why WS events miss the deep levels

Deep cards arrive via the `subagent_spawn` WS event. For a depth-1 child the
parent is literally `dashboard:<slot>` and routing works; for depth 2+ the parent
is `subagent:X`, which cannot be resolved to a root slot at event time
(registration ordering), so those cards land on a phantom slot. `/api/spawn` is
the deterministic authority — it always carries `parent` + `depth` per agent — so
the frontend reconstructs the tree from it rather than depending on WS ordering.

Three compounding bugs had to be fixed for that to work:

1. **Reconcile was remove-only** — it computed the rooted-here subtree but used
   it only to prune, never to add agents missed over WS. It now backfills.
2. **Poll was gated on `hasActive`** — a freshly loaded page has zero agents, so
   the recovery poll never ran and the backfill was dead code. The poll is
   ungated and runs on mount.
3. **`parentOf` was built from live agents only** — when an intermediate manager
   completed, a live deep agent's chain hit `undefined`, `rootsHere` returned
   false, and the subtree silently vanished as the cascade wound down. The index
   is built from **all** agents, including completed ones.

Completed nodes render dimmed rather than removed, so the spine never loses its
middle when managers finish out of order.

### One rooted set drives every number on the run card

`SubagentRunCard` derives its counts, total, settled state and depth from a
single "rooted at this launch" set. Mixing scopes is a real failure mode: a
launch-scoped count beside a subtree-scoped depth rendered `2 agents running ·
3 levels` — two numbers that cannot describe one set, since three levels needs at
least three agents. It also let the card report the wave *finished* while a
grandchild it spawned was still running, which is a wrong terminal state rather
than merely a wrong number.

Launch members keep their `undefined` holes in that set on purpose: `tally`
counts those as `unknown`, which is how the card knows an announced member is no
longer observable.

### Stale reconcile responses

The spawn list is fetched under one shared query key, so a single fetch serves
every pane. Each pane records the timestamp of its last run-boundary eviction and
drops any response whose request **started** at or before it, which prevents a
reconcile that was already in flight from resurrecting agents the run boundary
just cleared. The start time must travel *inside* the query payload — the arrival
timestamp is always after the eviction and cannot distinguish the two.

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `agent.subagent_tree_attribution` | `false` | Attribute nested children to their true parent, render the tree, and permit subagents to spawn. Off = flat tree, children told not to spawn (prompt-level). |
| `agent.subagent_max_per_session` | `0` (auto) | Node ceiling for one tree, every level plus queued. `0` = effective global cap; never unlimited; fails closed to `3`. |
| `agent.max_subagents` | `0` (auto) | Global concurrency cap. See [Dynamic Sub-Agent Sizing](dynamic-subagent-sizing.md). |
| `agent.subagent_auto_max` | `32` | Ceiling for the auto-sized global cap. |

## Known gaps

- `SubagentManager.root_slot_for` is **dead code** — written to route nested
  events to a root slot, never wired to a production caller.
- The `spawn_run`-only requirement of Part 4 is prompt-level; the blocking tool
  is still reachable from a subagent. A hard refusal for subagent callers would
  turn a possible long stall into an immediate error.
- On hosts with no memory probe (e.g. Windows) the auto global cap falls back to
  the floor and the per-spawn memory gate is inert by design — see
  [Dynamic Sub-Agent Sizing](dynamic-subagent-sizing.md). A small cap makes the
  Part 4 stall easier to reach, so pinning `subagent_max_per_session` below the
  global cap is prudent there.
