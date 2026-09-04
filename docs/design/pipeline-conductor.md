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
(one item / one worktree / one PR, six-prefix report protocol), the probe cycle and its action
table, independent green verification, the intervention ladder, outage recovery and loop liveness,
the adjudication and override protocol, the admission table, the credit budget rules,
steering-as-mode-change, and merge cleanup. The design puts the bookkeeping in three
subprocess-free scripts:

- `scripts/claim_preflight.py` — one deterministic verdict per candidate item, from five checks in
  one invocation: open PRs (fork PRs included), merged PRs tested for having actually landed on the
  default branch, prose self-claims and closure requests in the body and last comment (a closure
  request counting only from the reporter or a repository insider, since closing an item is a write
  driven by ingested text), the named symbol's presence on the default branch, and a
  recency/authorship risk flag. The verdict is a pure function of the checks, so every branch is
  unit-testable, and the exit codes are the interface: `CLAIM` / `SKIP` / `CLOSE` / `UNKNOWN`, where
  `UNKNOWN` is never reported as `CLAIM`. Two of those checks are deliberately non-vetoing: symbol
  absence downgrades to a high-risk claim unless the item is corroborated as bug-class, because a
  feature request names the symbol it proposes to add, and `risk=high` routes the item out of the
  batch to its own live recheck rather than merely annotating the line.
- `scripts/fleet_probe.py` — one call per cycle: tail-classifies every worker session, emits the
  tail's message index, computes idle age, flags error tails, distinguishes a terminal report from
  an idle one, scans for banned processes scoped to the fleet's own worktrees, and reads host load
  plus per-cycle delivery counters. Handled signals live in a state file the script owns. All paths
  are derived, never configurable: transcripts only from this gateway's own session store (keys are
  validated stems; a candidate resolving outside the store — a symlink — reads as missing), state
  only beside the config file.
- `scripts/credit_spend.py` — per-item credit rollup with budget verdicts `within` / `exhausted` /
  `truncated` (a bounded scan never claims `within`) / `unmetered` (absent metering is unknown,
  not zero).

The design rule the three share: **a decision expressed as prose in the skill rots silently, and a
decision computed by a script can be tested.** Each script exists because a predicate the skill
used to state in prose was found to be answering one question and treating an empty answer as
permission.

Decisions worth naming, in the skill's own sections:

- **Claim**: the preflight's verdict plus a four-way collision check (open PR, merged PR already on
  the base, branch, worktree), then an atomic claim — lock label and assignee in ONE call, because
  two calls leave a window another operator dispatches into. A batch preflight orders the queue; a
  per-item live recheck immediately before the claim is what authorizes it. An item that is open,
  claimed and already fixed is triage debt: the verdict is close-with-evidence, not dispatch.
- **Verification**: a worker's GREEN is never trusted — check-runs collapsed per lane (newest run
  wins), head SHA pinned, reviewer verdicts read from job logs and marker comments.
- **Progress vs liveness**: the probe reports an absolute per-session message counter, and an
  unchanged counter across two probes is no progress whether or not a turn is open. It is the one
  discriminator a self-deadlocked worker cannot fake, and it moves the next step from checking
  liveness to checking effect (did the artifact appear, did the remote head move). Absolute rather
  than window-relative on purpose: a window-relative index saturates once a session outgrows the
  tail bound and then reads as precisely the frozen counter the field exists to detect. It is also
  free, because the bundled probe's tail read slices a file it has already loaded whole — so
  `tail_bytes` caps how much is PARSED, not how much is read, which is worth stating because the
  name and the setting's own description both suggest otherwise.
- **Intervention**: nudge → a read-only inspector subagent (enforced via `allowed_tools`) →
  ruling: sharpened re-dispatch, adjudicate, open-issue descope, or reclaim. Two sessions on one
  item: decide ownership once. A terminal report is closed out, never nudged — a monitor loop is
  only correct while something external can still change.
- **Adjudication**: overrides only when every lane is settled, the finding is the sole red, the
  head SHA is pinned, the rationale is public, and the branch is push-frozen after. A base-owned
  red is proven three ways and fixed once for the whole fleet. When review rounds keep reopening in
  one function span, the convergent ruling is subtraction — consecutive rounds in one place indicate
  one unwritten contract, not N mistakes.
- **Governance**: delivery capacity is the primary admission instrument (below); the load/memory
  ladder ample/tight/critical is secondary; banned-operation response is stop + cooldown +
  directive re-injection, acting only on processes whose cwd is inside the fleet's worktree set.
- **Budgets**: a per-item credit allowance (default 100). Exhaustion triggers a burn review —
  progressing items get a recorded top-up, thrashing items get stopped, not refilled; past the
  top-up ceiling the decision escalates to the human with the burn history.

## Admission is sized on delivery, not on load

The obvious instrument for admission is the host's load average and free memory, and at fleet scale
it is the wrong one: the skill's admission section carries the causal claim and the posture table.
What belongs here is the design consequence. Because the saturating resource is invisible to the
instruments an operator reaches for first, the fix is not a better threshold on load but a DIFFERENT
counter, which is why the probe had to grow one: an admission rule can only be as good as the
cheapest signal that actually correlates with the ceiling. Load and memory stay in the ladder as a
secondary check, because a memory-starved host is a real condition too; they are simply not the
ceiling the fleet hits first.

The same reasoning caps the conductor's own forge calls. A dozen worker babysit loops and the
conductor all poll one account, so the conductor bounds its own PR sweeps per cycle, staggers them,
prefers REST over GraphQL and search, and runs the greens sweep only when the human is actually
approving. Rate limit is a shared, invisible resource, and the conductor is the only participant in
a position to see the aggregate.

## Conductor-owned state: `conductor-status/v1`

The session ledger records the work items. It does not record the conductor's own obligations, and
those turn out to be the ones that go missing, for a structural reason rather than a careless one:
the probe deliberately does not re-fire a signal already marked handled, and that suppression is
what keeps a quiet cycle at one line of output. A worker waiting on an adjudication therefore goes
silent BY DESIGN, and the debt the conductor owes it becomes invisible. The probe tracks the fleet;
nothing tracked the conductor.

`conductor-status/v1` is that store. The skill defines its fields — as a schema, so a rewrite cannot
quietly drop one; this document carries only why the two load-bearing ones exist. **`open_rulings`**
(`{worker, pr, question, asked_at}`) is reviewed every cycle independently of what the probe fired,
and an entry clears when the ruling is DELIVERED rather than when it is decided, because the silence
it covers is the probe working correctly. **`last_index`** holds the previous cycle's probe index, so
the no-progress test is a comparison against a recorded value rather than something the agent has to
remember across a compaction. The rest of the file is bookkeeping the human's "where are the other N"
question is answered from, plus the conductor's own task list; where it restates per-item state the
ledger already holds, the ledger is authoritative and the status file is a one-cycle cache.

Merge reconciliation follows from the same principle. It runs every cycle and **unfiltered** — one
`gh pr list --author <me> --state all` call, explicitly limited and listing merges since a
timestamp — rather than on a schedule against a hand-maintained set of PR numbers, because
filtering the reconcile to the PRs already tracked reproduces the original defect one level down: a
merge of a fleet PR that never made the watchlist cannot be seen by construction. The explicit
limit matters for the same reason the filter does: the default page is 30 and an over-long list is
trimmed silently, so a full page has to be read as truncation rather than as an answer.
`mergeable=UNKNOWN` fanning out across the open PRs stays useful as a secondary trigger meaning
"the base moved", never as the detector.

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
  conductor's) and the `pipeline-conductor` builtin skill with its three scripts, behavior pinned by
  tests (claim verdict precedence, probe classification, handled-set suppression, key containment,
  budget verdicts) and the skill's own contract pinned by tests over its text, so a rewrite cannot
  silently drop a clause the scripts depend on.
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
5. **External watchdog on loop liveness.** An approval or transport outage kills every monitor loop
   on the host, the conductor's patrol loop included, and the skill states that limit and closes what
   procedure can close: recovery is a fleet-wide resume-and-re-arm sweep, and the re-arm is
   conditional on there being something external left to watch. The residual hole needs a watcher
   OUTSIDE the loop, which is a product capability rather than a skill clause — hence a decision
   rather than a documentation gap.
