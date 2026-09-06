# Monitor architecture

## Purpose

One paradigm for every monitoring loop: watch an external subject, spend an
agent turn only when the subject has usefully changed, and stay bounded when it
never does.

The goal is a substrate, not a pull-request watcher. Any loop with an external
subject plugs in -- a pipeline run, a ticket, a deployment, an alarm, a queue
depth -- and pull requests are the **first subject family**, not the subject
matter. Where a layer below names a pull request it is naming the first
implemented family; the measure of this design is whether the next family costs
less than the first did.

This spec is the contract for the consolidation proposed in
[rfc-consolidated-monitor.md](../../request-for-change/rfc-consolidated-monitor.md).
It is written as the target, and the code does not yet match it everywhere, so
every section carries its status. Read the status table before trusting a
section as a description of what runs today.

Verified against the provider-neutral feature layer at `52f33de15`.

Two implementation specs sit under this one and describe what runs today:
[agent-interrupt-controller.md](agent-interrupt-controller.md) for the kernel
driving script-cron pollers, and [babysit-pr-watch.md](babysit-pr-watch.md) for
the pull-request watch built on it. Where either disagrees with a layer below,
this spec states the target and that one states the present.

| Layer | Status | Where it lives today |
|---|---|---|
| Subject and registry | `partial` | `monitoring/registry.py` owns kind/objective/capability data for four public pull-request kinds plus internal `gh-pr` and `github_workflow_run`; `probes/__init__.py` still has its separate dispatch branch |
| Probe | `partial` | `monitoring.models.MonitorProbe` and `MonitorProbeResult` are provider-neutral and plural; the `irq.Probe` path remains separate |
| Observation | `partial` | the `Observation` type and the `Severity` vocabulary live in `irq.py`; `PrWatchProbe` in `probes/gh_pr.py` emits the keys; `monitoring/` reduces a subject to one fingerprint |
| Decision | `partial` | `decide_monitor` returns a `MonitorVerdict` carrying its entries and remains pure, but is edge-triggered and has no coalescing; `irq.py` already level-triggers with a re-alert window and a coalescing floor |
| Persistence | `partial` | versioned in `monitoring/`; unversioned in `irq.py`, which also holds decision logic |
| Driver | `implemented` | in-session timer in `autonudge.py`; out-of-session script cron in `babysit/scripts/pr_watch.py` |
| Delivery | `implemented` | session directive keyed by the call's input digest, shared by both arming paths |

## Three prerequisites

Nothing in the seven layers is reachable until these three exist. Each is a
property of the code today, and each one blocks the substrate rather than merely
inconveniencing it.

### A verdict that can carry its evidence

**Status: implemented at the structured-monitor boundary.** `MonitorDecision`
remains the seven-value effect selector, but `decide_monitor` returns a
`MonitorVerdict(decision, entries)` and every consumer reads the selector through
that wrapper. The entries tuple is provider-neutral and plural, so adding more
evidence no longer requires changing the return type.

The live decision still places exactly one `MonitorObservation` in that tuple,
reduces the subject to one fingerprint, and lets `format_monitor_wake` compose
operator text from `MonitorState.last_observation` and `wake_instructions`. The
return-type prerequisite is therefore complete; per-condition entries,
coalescing, and delivery from those entries remain target work in layers 3, 4,
and 7.

### A probe boundary not typed to one implementation

**Status: implemented for structured monitors.** `MonitorProbe` in `models.py`
is the public, plural Protocol and returns
`Mapping[str, MonitorProbeResult]`. `run_shadow_probe` consumes it directly.
The controller's private `_Provider` mirrors that provider-neutral result and
adds only the `use_owner_credentials` capability its delivery path owns. GitHub,
GitLab, Azure DevOps, Bitbucket, and the workflow-run acceptance provider all
return the shared record; no boundary names a GitHub-specific result.

The remaining duplication is between the structured-monitor Protocol and
`irq.Probe`, not between provider implementations. Consolidation still has to
make those two drivers consume one extension point.

### A kind and objective vocabulary that is not one shared list

**Status: implemented for structured monitors.** `monitoring/registry.py` owns
one `MonitorKind` data row per kind. Each row declares its own objectives and
capabilities; `kind_supports_objective` enforces the pairing. The MCP schema,
validation schema, REST handler, arming path, and shadow path derive their
answers from that registry instead of maintaining independent allowlists.

Four pull-request kinds are publicly armable with `review_ready`. The internal
`gh-pr` kind and the `github_workflow_run` acceptance kind are registered but
not public; the latter alone declares `run_complete`. The `irq` inference path
still owns a separate `gh-pr` spelling, with `test_monitor_kind_registry.py`
pinning that the two vocabularies agree until consolidation removes the split.

## Two extension points, not yet one

There is an extension point, and an author adding a kind today conforms to it
rather than inventing it. `irq.Probe` is a base class whose docstring says
"Domain half of a watch. Subclass and implement both methods": two required hooks
raise `NotImplementedError`, and `tuning()` and `wake_suffix()` are optional
overrides. `PrWatchProbe` in `probes/gh_pr.py` conforms, and a second cron-path
kind still subclasses `irq.Probe` and adds its branch to `build` in
`probes/__init__.py`.

The `monitoring/` package now has a different extension point:
`models.MonitorProbe`, a structural Protocol with no behaviour inheritance, plus
the data-only kind registry. The four source-provider adapters and the workflow
run acceptance provider conform to it. The controller still owns the concrete
default provider map, while the shadow path receives a provider explicitly.

That is progress, not consolidation: a kind intended for both drivers still has
to integrate with two contracts. The remaining target is one plugin shape that
both drivers consume; the acceptance fixture below proves the structured half
is provider-neutral, not that the two stacks are already one.

## A monitor is a field, not a system

A reader who knows the code arrives expecting a monitor subsystem sitting beside
the nudge loop. There is no such thing. A monitor is a **nullable field on a
nudge loop**: `NudgeLoop` is one dataclass carrying `monitor: MonitorState | None`
alongside `gate: bool`.

`gate` is the discriminator, and the `monitor` field's own comment states the
rule: `gate=True` records belong to the prompt path, and controller-owned records
carry state with `gate=False`. So one class takes three shapes:

| `monitor` | `gate` | Shape |
|---|---|---|
| absent | either | a plain prompt loop with no probe state |
| present | `True` | an observation-gated prompt loop |
| present | `False` | a controller-owned structured monitor |

`is_structured_monitor_loop` selects the third by testing that `monitor` is
present and `gate` is false, and it is the guard that keeps the populations
apart everywhere they meet: the dashboard handlers, the session directive
application path, and the Slack gateway all branch on it.

What follows for this spec is that the substrate is not a new subsystem to build
beside the loop. It is a change to what that one field holds and to which
readers may look inside it.

## The seven layers

A monitoring loop is seven concerns, and each one has exactly one owner. The
value of the split is that six of them never learn what is being watched.

### 1. Subject

A subject is the external thing being watched. It is typed per kind and returns
a stable identity; two ticks naming the same thing must produce the same
identity, because identity is what state is filed under.

Kinds are registered as **data**, not as a branch. A new kind must not require
editing a dispatch function, because a dispatch branch is where every future
kind accumulates its special case. The structured path now has that registry;
the `irq` path's single-branch `build(kind)` is the thing consolidation still
replaces.

A subject is one addressable thing. A pipeline of several pull requests is not a
subject; it is several subjects sharing a watch.

### 2. Probe

The probe turns subjects into observations. Its signature is **plural from day
one**:

```
probe(subjects, budget) -> Mapping[SubjectId, ProbeResult]
```

`ProbeResult` carries the observations, the subject's current revision, whether
the read was complete, and a classified error or `None`.

This is the single most consequential contract in this spec. A per-subject probe
interface cannot be batched later without changing every implementation and
every caller, and batching is not a micro-optimization here: fifty subjects read
one at a time is roughly 150 process invocations against one query. Today
batching is reachable only from the single out-of-session poller, for no reason
other than the interface shape.

Rules:

- A probe that *can* batch **must**. A probe that genuinely cannot implements the
  plural signature and loops internally, so the caller never encodes the
  difference.
- One query per (host, credential) per tick. Subjects sharing a credential share
  the query.
- A partial failure degrades only the subjects it covers. One unreadable subject
  must not fail the batch.
- Every error is classified before it leaves this layer. An unclassified failure
  is `unknown` and counts as not passing.

### 3. Observation

An observation is a named entry. The type already exists in the kernel as
`Observation(key, severity, brief, epoch_scoped)` in `irq.py`, with `Severity`
carrying `WAKE`, `TERMINAL` and `NMI`. What this spec asks for is a **rename, not
a new type**:

```
Observation(key, severity, resets_on, brief="")
```

`epoch_scoped: bool` becomes `resets_on`, and `NMI` becomes `IMMEDIATE`. Naming
that plainly matters, because a rename has existing callers -- `PrWatchProbe` in
`probes/gh_pr.py` constructs these today -- so the migration is a mechanical
rewrite of live code rather than a greenfield addition. The reason for each new
name is that the old one describes the implementation and the new one describes
the condition: `epoch_scoped` says which bookkeeping bucket an entry falls in,
while `resets_on` says what clears it, and `NMI` borrows an interrupt term for
what is really an urgency claim.

- **`key`** is a semantic string, stable across ticks, never a hash. `conflict`,
  `red:<check>`, `ready`, `comment:<id>` -- the vocabulary `PrWatchProbe` emits. A
  hash cannot be deduplicated per condition, cannot be coalesced with a sibling,
  and cannot be re-asserted, because nothing can tell whether two hashes describe
  the same condition.
- **`severity`** is `WAKE` (foldable into a coalesced wake), `TERMINAL` (an end
  state: deliver and retire the watch), or `IMMEDIATE` (bypasses the coalescing
  delay but not the budget, for a condition where waiting observes nothing
  further -- a conflicted pull request dispatches no checks, so a pending count
  never drains). The kernel already implements this behaviour under the name
  `NMI`.
- **`resets_on`** is `REVISION` when a new revision clears the condition, or
  `NEVER` when it belongs to the subject rather than the revision. A comment
  survives a force-push; a failing check does not. `epoch_scoped` is the same
  distinction expressed as a boolean over the kernel's epoch.
- **`brief`** is operator-facing text, delivered only if the entry wakes someone.

A subject's fingerprint, where one is still needed, is **derived from** the
entries. There is one source of truth, so the two cannot disagree.

### 4. Decision

A pure function. No IO, no subprocess, no reading the clock -- the clock arrives
as a value:

```
decide(entries, prior_state, budgets, now) -> Verdict
```

`Verdict` is `Quiet`, `Wake(entries, brief)`, `Terminal(entries)`, or
`Stop(reason)`.

This layer knows nothing about pull requests, hosts, or agents. That is
verifiable rather than aspirational: `decide_monitor` in `monitoring/decision.py`
names no host and reaches no IO, its only imports are the state and observation
models, and its clock arrives as the `now` parameter. `test_monitor_decision.py`
exercises it with no network and no filesystem, and that property is what makes it
the skeleton the rest is merged into.

Evaluation order is part of the contract, because the order is what makes it
fail safe:

1. **Terminal** short-circuits everything. A merged subject is not triaged as a
   failure.
2. **Budget** exhaustion yields `Stop`. Checked before classification so an
   expensive classification cannot be what exhausts the budget.
3. **Per-key dedupe** against the re-alert window.
4. **Coalescing window**.
5. **Floor**.

The engine is **level-triggered**, not edge-triggered, and one of the two already
is. `irq.py` level-triggers on the live cron path today: per-key `alerted`
timestamps in its loaded state, `_dedupe_key` distinguishing epoch-scoped from
sticky entries, a re-alert window defaulting to six hours through
`DEFAULT_REALERT_SECS`, a coalescing window through `coalesce_secs`, and
`Severity.NMI` documented as bypassing the delay but not the mask. So
re-assertion-after-a-window is **not** a behaviour the system lacks; it is a
behaviour `monitoring/decision.py` lacks, and consolidation is where it stops
being available on only one path.

Each key carries its own alert timestamp, and a key that is still true re-asserts
once its window has elapsed. Edge triggering loses any condition that stayed true
across a wake that did not happen -- a busy session, an exhausted budget --
because on the next tick it is no longer a change. Level triggering is also what
the industry converged on: a Kubernetes controller reconciles observed against
desired rather than
consuming events, and Prometheus re-sends a firing alert and lets the receiver
deduplicate.

The re-alert window is what makes level triggering affordable, and the budget is
what makes it safe. A notification pipeline aimed at humans needs no token
budget because a paged human self-limits; an agent does not, which is why the
budget half of this design is not optional.

**The stall streak is engine state.** A watch whose verdict has been byte-identical
across N settled ticks with no progress is stuck, and the engine stops it. That
counter belongs in the persisted state the engine reads, not in a file that only
an instruction knows to maintain -- an advisory counter maintained by prose is
lost to exactly the long-running compaction it exists to survive. No engine state
holds a streak of **identical verdicts** today. Streak counters do exist and are
about something else: `quiet_streak` with `floor_ticks` counts consecutive quiet
observations and the deliveries they force, `consecutive_provider_errors` counts
provider failures, and `irq.py` carries its own consecutive-error backstop. None
of them notices a watch that keeps reaching the same conclusion.

#### The decision is split, and only half of it is pure

`decision.py` holds the **content** policy: did the subject change (its
fingerprint against `last_wake_fingerprint`), is the budget spent
(`monitor_budget_reason`), is this error retryable (`_provider_error_decision`
against `_RETRYABLE_PROVIDER_ERRORS`). It takes the clock as a value through its
`now` parameter and performs no IO at all.

The **delivery** policy is the other half, and it is impure. It lives in
`MonitorController.tick`, which decides whether a wake is already in flight
(`wake_in_flight`), whether the last dispatch came back busy (`wake_delivery`
holding `MonitorDispatchResult.BUSY`), and whether the evidence deadline has
passed (`completion_evidence_deadline`). That method runs the probe off-thread and
reads the wall clock through its own injected clock, so it cannot be tested the
way `decide_monitor` can.

Two facts about the wiring surprise a reader who goes looking for the decider:

- The decider is reached through the service, not from the controller. The
  controller calls `apply_monitor_probe`, and that is what calls `decide_monitor`;
  a reader who opens `controller.py` expecting the decision finds only
  `monitor_budget_reason`. Tracing the live path means going through the service to
  reach the place the verdict is made.
- `terminal_decision_for_outcome` documents its own dead branches. Its docstring
  records that `apply_monitor_probe` refuses a monitor with a recorded outcome
  **before** `decide_monitor` runs, flattening every terminal outcome to
  `STOP_BLOCKED`, so the verdict the delivery controller reports is not the one
  that function computes; `run_shadow_probe` on the persistence-only shadow path is
  the caller that still reaches them. Consolidation is where that flattening goes
  and the branches become live, which is why the function is named here rather
  than treated as dead code to delete.

Consolidation therefore merges two halves with different testability. It does not
lift one already-pure function into place.

### 5. Persistence

One versioned document per watch, written atomically at mode 0600, filed under a
digest of (kind, subject identity, watch id).

Required contents:

| Field | Why |
|---|---|
| `version` | every bump ships a migration; an unrecognized version is quarantined, never guessed at |
| `revision` | what `resets_on: REVISION` is measured against |
| `alerted` | per-key alert timestamps -- the level-triggered state |
| `coalescing` | the open window: when it opened, which keys joined |
| `errors` | per-kind counts, so a retryable class stays bounded |
| `budgets_spent` | turns, tokens and provider errors already charged |
| `stall` | the verdict digest and its consecutive-match count |

**The state document holds delivery bookkeeping only. It never holds subject
state.** What the subject looks like belongs in the evidence file, which is
disposable and regenerated per wake. This boundary is load-bearing in a way that
is easy to get wrong: a reader who assumes the state file holds subject state
will try to read the subject out of it and find only alert timestamps.

The document is rewritten whole and atomically, so it is a snapshot rather than
a log. Anything that needs a history needs its own append-only file.

### 6. Driver

A driver decides when a tick happens. Two are supported, and the difference
between them is a **capability**, not a configuration preference:

| Driver | Owns a chat slot | Can inject a turn | Runs with session trust |
|---|---|---|---|
| In-session timer | yes | yes | yes |
| Out-of-session poller | no | no, notification only | no, deny-by-default |

The rule that follows is absolute: **an out-of-session driver is a detector,
never a reactor.** A cron turn has no owning slot, so its tool calls land on a
deny-by-default approval path and time out -- while a denied tool inside a
completed turn still records the job as healthy. A design in which a cron fixes
something reports success and does nothing.

Both drivers are needed. Out-of-session detection reaches subjects with no live
session, and in-session injection is the only path that can act.

The in-session timer counts its interval from the end of its own last cycle
toward a fixed deadline, so a user message defers a due fire without restarting
the countdown. The real cadence is therefore the interval plus each cycle's own
duration, which callers must size for.

### 7. Delivery and turn injection

A verdict becomes at most one agent turn, through a fixed sequence:

1. Spill the evidence to a file.
2. Inject **one** turn carrying a summary and the path to that evidence -- never
   the raw payload. A wake that inlines its evidence pays for it in the session's
   context on every subsequent turn, because history is replayed.
3. Charge the wake **after** the turn completes and its usage is known.

Degradation is a ladder, and each rung is a different outcome rather than a
retry of the one above: a live slot takes the turn; a busy slot queues it; a slot
that is gone gets a notification instead. Headless delivery cannot start a
session, so a watch whose session is gone must not claim it woke anyone.

Deduplication is per (subject, key, revision): one wake per condition per
revision, and a re-alert only through the window. Transport is the session
directive selected by the digest of the call's own input, which both arming paths
already share.

## Rules the engine enforces, not the prose

An operational rule that lives only in an instruction can be violated silently.
These are code -- or, where marked `target`, are the reason this consolidation
exists, because deleting the instruction before the engine enforces the rule
leaves it enforced nowhere:

- An unclassified provider state is `unknown` and counts as **not passing**.
- Superseded attempts collapse to the newest per check identity. A host keeps
  cancelled earlier attempts in its rollup, and counting them reports a live
  failure that no longer exists. The two current implementations **disagree on
  this today**: the skill's status tool collapses to the newest attempt per
  identity in `collapse_superseded`, while the structured provider gives each
  `CheckRun` row a per-row group key in `_normalize_checks` -- `StatusContext` rows
  it does group, by context -- and maps `CANCELLED` to failed in `_normalize_check`.
  Since the state fold prefers `failed`, a superseded cancelled attempt reads as a
  live failure and can wake on a phantom.
- A published aggregate verdict is authoritative over the individual rows. A
  reader that only enumerates rows can report green while the aggregate is
  pending, which is not a hypothetical: a subject has been observed with every
  individual check complete and green while the aggregate context still read
  pending. Enforced today only in the status tool, which resolves the aggregate
  through `resolve_readiness_context`; the structured provider has no aggregate
  notion at all (`target`).
- A stale reviewer stamp is an entry (`stale:<name>`), not a paragraph.
- An un-dispositioned finding is an entry, so readiness cannot be declared over
  one.
- The stall streak is engine state, so a stuck watch stops itself (`target` -- no
  engine state holds it today).

## Adding a new monitored kind

The extensibility test is mechanical: adding a kind must not touch layers 4
through 7.

**What this looks like today, before consolidation lands.** On the cron path,
subclass `irq.Probe`, implement its two required hooks, and add a branch for the
kind to `build` in `probes/__init__.py`. On the structured path, register a
`MonitorKind`, implement the plural `MonitorProbe` contract, and wire that
provider into the controller and/or shadow caller whose capability the registry
declares. A kind that must reach both drivers still pays for both integrations.
The numbered steps that follow describe the unified target, not the current
tree.

1. Define the subject type with a stable `identity()`.
2. Register the kind as data in the registry.
3. Implement the plural probe. Emit **named entries**. Do not emit a fingerprint;
   the shared layer derives one.
4. Add nothing to the decision engine. If a new kind seems to need a change
   there, the entry vocabulary is wrong -- express the condition as a key and a
   severity instead.
5. Ship fixtures and a golden entry table: for each fixture, the exact entries
   expected.
6. Prove it: the decision engine's own tests pass **unchanged**. That is what
   demonstrates the kind is pluggable rather than special-cased.

A kind that cannot be added without editing layer 4 is a design defect in this
spec, and should be reported as one rather than worked around with a branch.

### The acceptance test

**Structured-path status: implemented.** `github_workflow_run` is registered as
non-public with its own `run_complete` objective; its provider returns the shared
`MonitorProbeResult`, and `run_shadow_probe` consumes it without a
GitHub-workflow branch in the decision layer. `test_github_workflow_run_monitor.py`
and `test_monitor_kind_registry.py` pin the result boundary, objective scoping,
and capability declaration. This proves the three prerequisites above for the
structured path. It does not yet prove driver consolidation, because the `irq`
path remains separate.

The six steps above are a procedure, and a procedure cannot fail. This is the
test, stated so that it can:

**Add a GitHub Actions workflow run as a second kind, and change nothing in the
shared layers.**

That is the right second kind because it shares the credential and the CLI, so it
adds no authentication work, while still being a genuinely different subject: a
different set of terminal states, and an objective that is not `review_ready`.

It passes only if all four hold:

1. No shared decision code changed.
2. No shared result type changed.
3. No shared protocol changed.
4. The decision engine's existing tests pass **unchanged**.

If it cannot pass, the prerequisites were wrong and they get fixed there. A branch
added for the new kind at that moment is not a shortcut -- it is the whole
substrate failing quietly, because the kind after it adds the next branch and the
shared layers become the dispatch table this spec exists to remove.

## What this does not cover

The substrate covers loops with an **external subject to probe**. That is the
whole boundary, and it is deliberate.

A conductor patrolling its own session has no external subject. There is nothing
to fingerprint, no revision that advances, and no host to ask for a verdict, so it
stays on the timer path. That is not a gap to close later. Every guarantee here --
identity, revision, level triggering, per-condition dedupe -- is stated in terms
of a subject, and a substrate whose subject is optional has no subject.

## Anti-patterns

| Pattern | Why it fails |
|---|---|
| One fingerprint per subject | cannot say what changed, cannot coalesce siblings, cannot re-assert a condition |
| Per-subject probe signature | cannot be batched later without changing every caller |
| Subject knowledge in the decision layer | every new kind then needs a branch there, and the layer stops being testable in isolation |
| Subject state in the state document | the document is a snapshot of delivery bookkeeping; a reader looking for subject state finds timestamps |
| A cron that reacts | no owning slot means deny-by-default tool calls that time out while reporting healthy |
| Conflating a throttle with a fault | a secondary rate limit can be refused while the primary counters read full, so a loop driven off an exit code escalates a transient throttle or treats it as terminal |
| Treating a cycle cap as a finish line | a loop that stops at its cap is indistinguishable from one that converged early, and bills for the difference |

## Known deviation

Every wake re-injects into the **same** session, so its context grows for the
life of the watch. The prevailing pattern elsewhere is a fresh context per wake,
and a durable-execution engine names the timer loop accumulating one history as
an anti-pattern outright. Both current implementations share this deviation, and
it is not resolved here: the change is larger than this consolidation and belongs
in its own proposal. It is recorded so a reader does not mistake the omission for
an argument that same-session wakes are correct.
