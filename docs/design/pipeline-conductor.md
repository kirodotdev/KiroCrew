# Pipeline Conductor: the issue pipelines as a conductor use case

A dedicated `kirocrew-pipeline-conductor` agent and a `pipeline-conductor` builtin skill that run a
repository pipeline as a supervised worker fleet. The skill is the operating procedure of record;
this document carries the intent and the decisions.

## What a conductor is

A conductor is a long-lived supervisor agent that owns a body of work end to end without doing any
of it itself: it picks up work items, stands up one worker session per item, patrols the fleet,
verifies results independently, rules on exceptions, and reports upward. Every deterministic step
(probing, verification arithmetic, budget sums) is delegated to bundled zero-token scripts; every
judgment (adjudication, intervention, re-planning) stays in the agent. The shape is **agent +
agent skills**: the skill carries the procedure, the scripts carry the bookkeeping, the agent
carries the judgment.

## Why

Kiro Crew's issue-fixing automation today is three open-loop pipelines (issue → new PR, red PR →
green, green PR → merge). They are **agentic workflows wrapped in deterministic scripts**: cron
scanners and dispatchers own the control flow; agent sessions do the work inside it. The AI-native
shape is the inversion — **deterministic scripts wrapped in an agentic control plane**, which is
exactly agent + agent skills.

The refactor is feasible now because most of the conductor architecture already exists in Kiro
Crew (see the facts below). What is missing is a conductor **specialized to run a pipeline**, and
the operating procedure that encodes how.

Why invert at all? A pipeline's control plane needs **flexibility**, which the agentic layer has
and a script does not:

1. **Direction correction.** Issues routinely attract over-complicated implementations. A script
   pipeline is only a dispatcher; a conductor detects the drift and corrects mid-flight — a
   sharpened re-dispatch, a descope, an open-issue conversion.
2. **Probes and live resource governance.** Worker sessions are noisy neighbours — one full-suite
   test run can starve the host. A conductor reads live posture and intervenes; a cron cannot even
   see the problem between ticks.
3. **Judgment on whether the work should happen at all.** Wrong premise, superseded, or a real
   design decision: a dispatcher cannot decline work; a conductor stands down with evidence or
   converts it into a proposal. A decorative fix is worse than no fix.
4. **Self-improvement.** A conductor summarizes as it operates and folds lessons back into its own
   skill, briefs and probe rules. A cron script cannot get better at its job.
5. **A node in the agent network.** A conductor is itself an agent, so other agents and crews can
   invoke it. Every future multi-agent capability inherits the pipeline for free.

The rigidity costs are concrete — the failure classes of the open-loop pipelines: duplicate
dispatch ending in mutual-yield deadlocks; silent stalls noticed only by the 90-minute heartbeat
reaper; every exception terminating in a human excavating logs; and hand-edited progress state
drifting from the forge.

With a conductor the human appears exactly twice: the merge click, and genuine design decisions.

## Verified facts this plan rests on

- The conductor pattern is already shipped: `kirocrew-conductor` is a generated agent spec, built
  at gateway boot, with per-verb dashboard grants and installer tests.
- The session-control surface exists: the `kirocrew-dashboard` MCP server (session
  create/read/send/stop, chat folders), opt-in per agent.
- The patrol loop exists (`monitor_start`, deadline-preserving, survives gateway restarts), as do
  the durable session ledger and live host posture (`resource_status`: ample/tight/critical).
- Per-session spend is measurable: usage shards record per-turn `credits` keyed by session slot.
  Only dashboard-chat turns are instrumented today — subagent and non-chat sessions burn
  invisibly, so budget verdicts must treat absent metering as unknown, never as zero.
- The dispatch queues are already sharded per `owner__repo`, so per-repository pipeline identity
  (#6221) has its storage seam half-built.
- Worker transcripts are tailable on disk, which is what makes one batch probe per patrol cycle
  cheap — the design that keeps a quiet cycle at one output line.

## The agent

`kirocrew-pipeline-conductor` follows the generated-agent pattern of `kirocrew-conductor` and its
security invariants:

- **No file-writing tool** (`fs_write`, `code` absent): never doing a work item's work itself is a
  spec property, not a prompt request.
- **Every auto-approval is a named verb**, dashboard and core alike: creates/reads, the patrol
  loop's own lifecycle, and owner reporting are granted; anything that mutates a peer session or
  starts new work from ingested content (`session_send`, `session_stop`, `spawn_run`, `task_run`,
  `workflow_run`, `cron_add`, `execute_bash`) stays mounted but gated. The conductor ingests
  untrusted content (issue text, PR bodies) on unattended cycles by design.
- **Unattended operation is a session-level trust grant** by the operator ("trust before seed",
  the same rule the worker sessions already follow) — not a standing spec-level bypass.

## The harness

The `pipeline-conductor` skill is the operating procedure: idempotent pickup, the work-order brief
(one item / one worktree / one PR, six-word report protocol), the probe cycle and its action
table, independent green verification, the intervention ladder, the adjudication and override
protocol, the resource-posture table, the credit budget rules, steering-as-mode-change, and merge
cleanup. Two subprocess-free scripts do the bookkeeping:

- `scripts/fleet_probe.py` — one call per cycle: tail-classifies every worker session, computes
  idle age, flags error tails, scans for banned processes, reads host load. Handled signals live
  in a state file the script owns. All paths are derived, never configurable: transcripts only
  from this gateway's own session store (keys are validated stems; a candidate resolving outside
  the store — a symlink — reads as missing), state only beside the config file.
- `scripts/credit_spend.py` — per-item credit rollup with budget verdicts `within` / `exhausted` /
  `truncated` (a bounded scan never claims `within`) / `unmetered` (absent metering is unknown,
  not zero).

Decisions worth naming, in the skill's own sections:

- **Verification**: a worker's GREEN is never trusted — check-runs collapsed per lane (newest run
  wins), head SHA pinned, reviewer verdicts read from job logs and marker comments.
- **Intervention**: nudge → a read-only inspector subagent (enforced via `allowed_tools`) →
  ruling: sharpened re-dispatch, adjudicate, open-issue descope, or reclaim. Two sessions on one
  item: decide ownership once.
- **Adjudication**: overrides only when every lane is settled, the finding is the sole red, the
  head SHA is pinned, the rationale is public, and the branch is push-frozen after. A base-owned
  red is proven three ways and fixed once for the whole fleet.
- **Governance**: posture ladder ample/tight/critical → dispatch / hold admission / stop the
  expensive items; banned-operation response is stop + cooldown + directive re-injection, acting
  only on fleet-owned processes.
- **Budgets**: a per-item credit allowance (default 100). Exhaustion triggers a burn review —
  progressing items get a recorded top-up, thrashing items get stopped, not refilled; past the
  top-up ceiling the decision escalates to the human with the burn history.

## The template: PipelineSpec

The bigger vision: **one conductor engine, configured per (repository, campaign) pair** — any
repo, any task. Every repo- or task-specific fact the conductor consumes is data:

```yaml
id: issue-fix                # queue/folder/audit names derive from it
repo: <owner>/<repo>         # per-repo object key (#6221); one today, list later
default_branch: main
work_source: {kind: gh_issues, select_labels: [...], skip_signals: [...]}   # SEAM: adapter
claim: {lock_label, marker_phrase, operator_tag}                            # lock protocol as data
worker_contract: {branch_pattern, worktree_pattern, brief_template, heartbeat_sla}
verifier: {gate_profile, reviewer_lanes: [...], readiness_context, acceptance}  # SEAM: adapter
adjudication: {auto_action_categories, blast_radius_max, security_denylist}     # SEAM: policy as data
governance: {max_in_flight, per_cycle, credit_budget_per_item, session_ceiling, posture}
interface: {folder_name, worker_model, digest_language, notify_channel}
```

Five seams, each an interface with one implementation shipping: the work-source adapter (a docs or
test-coverage campaign is a new work source + a new brief, not new engine code), the verifier
adapter (reviewer lanes as a data list), protocol vocabulary as data (labels, markers, naming —
the highest-leverage seam), adjudication policy as data, and per-repo identity (#6221). One
invariant stays out of the template: the forge is the cross-operator lock (claim label + assignee
+ operator-tagged comments); local state is only a cache.

## Phases

- **M0** — the agent and the harness: the generated `kirocrew-pipeline-conductor` spec (installer,
  filename constant, roster hiding, docs registry, installer tests mirroring the existing
  conductor's) and the `pipeline-conductor` builtin skill with its two scripts, behavior pinned by
  tests (probe classification, handled-set suppression, key containment, budget verdicts).
- **M1** — `PipelineSpec` file consumed by the scripts; SQLite event store (append-only `events`,
  `issues` as a fold, `decisions` first-class, `dispatch_id UNIQUE`); the board becomes a
  generated view.
- **M2** — adjudication queue, SLA timers, retry/catch matrix as data; budgets enforced, not just
  observed; the self-improvement loop — per-run lessons folded back into the skill and briefs.
- **M3** — `baking` stage on main CI (merged ≠ done); compensation sagas; per-repo pipeline
  objects under Issue Radar (#6221); the conductor exposed as an invocable node for other agents
  and crews.

## Open decisions

1. **Resident session vs deterministic spine.** A patrol loop is overwhelmingly no-signal
   bookkeeping, which argues for a deterministic engine with the LLM demoted to stateless
   judgment calls; a resident session's accumulated context makes some adjudications sharper. The
   M1 event store is what makes the spine option real, so the call is deferred until it exists.
2. **Unattended `session_send`/`session_stop`.** Session-level trust is the shipped answer; if
   approval prompts prove to be the attended-mode bottleneck, a follow-up can argue a narrowed
   server-side capability (e.g. send restricted to conductor-created sessions).
3. **Credit metering coverage** for subagent and non-chat turns — folded into the planned
   per-request token metrics work; budgets meanwhile bind what the shards see.
4. **Digest surface**: chat today; a merge-queue/proposal-queue report page is an Auto Triage
   Pipeline app extension once the event store exists.
