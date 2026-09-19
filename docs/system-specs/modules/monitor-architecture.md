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
| Probe | `partial` | `monitoring.models.MonitorProbe` and `MonitorProbeResult` are provider-neutral and plural, and `monitoring/github_pull_request.py` batches its subjects into one GraphQL document per evidence kind; the other adapters loop internally and no driver assembles a batch, and the `irq.Probe` path remains separate |
| Observation | `partial` | the `Observation` type and the `Severity` vocabulary live in `irq.py`; `PrWatchProbe` in `probes/gh_pr.py` emits the keys; `monitoring/` reduces a subject to one fingerprint |
| Decision | `partial` | `decide_monitor` is IO-free but state-mutating: it coalesces successive changes to one subject over time through a window on `MonitorState` (a floor and a head-change reset) and derives its dedup comparison so an unresolved change re-asserts on a re-alert interval. It writes the window fields on the staged state and READS the alert map; the caller stamps the alert map on a wake and persists the same staged state, so decide-and-persist is a required pairing. `irq.py` keeps its own multi-signal coalescing for the cron path |
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

The live decision places exactly one `MonitorObservation` in that tuple, reduces
the subject to one fingerprint, and lets `format_monitor_wake` compose operator
text from `MonitorState.last_observation` and `wake_instructions`. That text is
kind-dispatched: `format_monitor_wake` takes the subject's `kind` from
`MonitorState.kind` -- the authoritative armed record, not the provider-supplied
canonical -- reads that kind's registry entry for its subject noun and field
list, and renders those fields, so a non-pull-request kind reads as what it is.
The return-type prerequisite is complete; the engine coalesces successive changes
to that one subject over time and re-asserts an unresolved change on a re-alert
interval, both through a window on `MonitorState`. Per-condition entries and the
multi-signal fold across simultaneous conditions remain target work in layers 3
and 4, since one fingerprint per subject has no second condition to fold.

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
one `MonitorKind` data row per kind. Each row declares its own objectives, its
capabilities, and how it describes itself when it wakes a session -- a subject
noun and an ordered `wake_fields` list, held as strings; `kind_supports_objective`
enforces the objective pairing and delivery reads the entry's noun and fields
through `monitor_kind`. The MCP schema, validation schema, REST handler, arming
path, and shadow path derive their answers from that registry instead of
maintaining independent allowlists, and delivery reads the same table for the
noun and fields rather than hardcoding one kind's shape.

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

### Retiring `irq.Probe` is gated on layer 3

Retirement is downstream of layer 3, not of the coalescing window the pure
decision engine gained. That window folds successive changes to one subject over
time, which is not the mechanism the cron path depends on. What that path uses,
and the shared engine cannot yet express, is already named in layers 3 and 4
below: the urgency claim layer 3 and the kernel now both call `IMMEDIATE`, for a
condition where waiting observes nothing further; and the
per-entry `resets_on` distinction, which decides whether a new revision clears an
entry or the entry outlives it. One fingerprint per subject can express neither.
It has no per-entry identity to scope and no severity to raise, so an entry that
must fire now cannot say so, and an entry that survives a force-push cannot be
told from one the force-push resolved.

Deleting the old extension point before layer 3 lands would therefore delete
those two behaviours rather than move them. Until then the two drivers keep two
contracts, and an author adding a cron-path kind still subclasses `irq.Probe`.

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
one at a time is roughly 150 process invocations against one query.

**Status: the GitHub pull-request probe batches; no caller passes more than one
subject yet.** `GitHubPullRequestProvider.probe` spends one GraphQL document per
evidence kind per chunk of at most 25 subjects, so a tick of any size up to that
bound costs three requests instead of three per subject, and each further chunk
adds three. A read the GraphQL point budget REFUSES costs more than that, because
the fallback below is per subject rather than per chunk. It carries the (host,
credential) rule as a check rather than as a grouping pass: the credential is the
call's own argument, and a chunk is refused if it names two hosts. The
other four adapters still loop internally and declare so in their own docstrings.
What is missing is above the probe, not inside it: the in-session driver arms one
`asyncio` task per loop in `autonudge.py`, so a tick structurally sees one
monitor, and the out-of-session poller runs one subject per cron job through
`irq.Probe.observe`, which is singular. A batch therefore has no assembler; that
is a driver change, and it belongs with the consolidation rather than with the
probe.

#### Two API budgets, not one

GitHub meters GraphQL in points and REST in requests, and the two budgets are
SEPARATE. A probe bound to one bucket therefore retires on
`max_provider_errors` whenever that bucket is spent, while the other bucket is
untouched and answering — a healthy subject reported as a provider outage. The
GitHub pull-request adapter answers a **rate limit, and only a rate limit**, by
re-reading the subject on the other bucket:

| Fact | GraphQL | REST fallback |
| --- | --- | --- |
| lifecycle, draft, head, mergeability | primary document | `repos/{o}/{r}/pulls/{n}` |
| review decision | `reviewDecision` | none — reported `unknown` |
| commit statuses | rollup `StatusContext` | `commits/{sha}/status` |
| check runs | rollup `CheckRun` | none — see below |
| review threads | `reviewThreads` | none — count reported incomplete |

Two bounds make this safe rather than merely more available:

- **A degraded observation cannot be a ready one.** What REST cannot express is
  reported as INCOMPLETE evidence, never guessed at, and
  `classify_pull_request_facts` answers incomplete checks, incomplete threads and
  an unknown review decision with `PENDING`. So the fallback can still wake a
  session on a red board and can never call a subject review-ready on half of
  one.
- **Check runs are deliberately not read on REST.** REST names no workflow for a
  check run, while the rollup builds a check run's identity as
  `"<workflow> / <name>"`, so a check run read there would carry a different
  identity for the same check and the watch would report one failure twice, once
  per transport. A commit status carries `context`, which IS its GraphQL
  identity, so the status half is exactly the half readable without that drift.

A subject the fallback cannot read either KEEPS the rate limit it was already
charged, so the second diagnosis can never substitute a terminal kind for a
retryable one. That is what makes the fallback never worse than no fallback.

Rules:

- A probe that *can* batch **must**. A probe that genuinely cannot implements the
  plural signature and loops internally, so the caller never encodes the
  difference.
- One query per (host, credential) per tick. Subjects sharing a credential share
  the query. An adapter satisfies this with a CHECK, not with a grouping pass: a
  pass that sorts subjects into per-host queries is machinery for a case its own
  target gate cannot construct, so it would ship unexercised, while a check is
  exercised on every call and fails closed the day a second host is accepted. A
  read whose failures are separate is a separate query: the GitHub
  adapter keeps its load-bearing primary read apart from its two supplemental
  ones, because a document that selects the check rollup hands its lifecycle
  facts to a missing Checks permission.
- A partial failure degrades only the subjects it covers. One unreadable subject
  must not fail the batch.
- A refusal that names a spent BUDGET rather than a fault is retried on any other
  budget the provider meters separately, before it is charged. Every other failure
  is charged as it was: a missing subject is missing on both buckets and a rejected
  credential is rejected on both, so a second transport only makes those slower to
  report.
- Every error is classified before it leaves this layer. An unclassified failure
  is `unknown` and counts as not passing.

### 3. Observation

An observation is a named entry, and the kernel type is now the one this layer
asks for. `irq.py` carries it as:

```
Observation(key, severity, brief="", resets_on=ResetsOn.REVISION)
```

with `Severity` carrying `WAKE`, `TERMINAL` and `IMMEDIATE`. This landed as a
**rename of the existing type, not a new one**: `epoch_scoped: bool` became
`resets_on: ResetsOn`, and `NMI` became `IMMEDIATE`. `PrWatchProbe` in
`probes/gh_pr.py` constructs these, so it was a mechanical rewrite of live code
rather than a greenfield addition.

Each new name describes the condition where the old one described the
implementation. `epoch_scoped` said which bookkeeping bucket an entry fell in;
`resets_on` says what clears it. `NMI` borrowed an interrupt term for what is
really an urgency claim.

`resets_on` is an enum rather than a renamed boolean, because the field answers
*what clears this* and a boolean can only answer *yes or no*: read as a flag,
`resets_on=False` would have to mean "does not reset on -- nothing", the opposite
of what `NEVER` says. The kernel already stored the distinction as one of two
named key spaces, so a two-member enum is also what makes the in-memory type and
the persisted encoding the same shape.

`brief` stays ahead of `resets_on` positionally, which is not the order this
section first sketched. Both fields have defaults, so nothing is gained by moving
`resets_on` forward, and reordering would silently redirect every existing
three-positional call rather than fail at it.

- **`key`** is a semantic string, stable across ticks, never a hash. `conflict`,
  `red:<check>`, `ready`, `comment:<id>` -- the vocabulary `PrWatchProbe` emits. A
  hash cannot be deduplicated per condition, cannot be coalesced with a sibling,
  and cannot be re-asserted, because nothing can tell whether two hashes describe
  the same condition.
- **`severity`** is `WAKE` (foldable into a coalesced wake), `TERMINAL` (an end
  state: deliver and retire the watch), or `IMMEDIATE` (bypasses the coalescing
  delay but not the budget, for a condition where waiting observes nothing
  further -- a conflicted pull request dispatches no checks, so a pending count
  never drains).
- **`resets_on`** is `ResetsOn.REVISION` when a new revision clears the condition,
  or `ResetsOn.NEVER` when it belongs to the subject rather than the revision. A
  comment survives a force-push; a failing check does not. The kernel stores a
  `REVISION` key in its epoch space and a `NEVER` key in its sticky one, which is
  the distinction `epoch_scoped` expressed as a boolean over the epoch.
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

The engine is **level-triggered**, not edge-triggered, on both paths. `irq.py`
level-triggers on the live cron path: per-key `alerted` timestamps in its loaded
state, `_dedupe_key` distinguishing `REVISION` from `NEVER` entries, a re-alert
window defaulting to six hours through `DEFAULT_REALERT_SECS`, a coalescing
window through `coalesce_secs`, and `Severity.IMMEDIATE` documented as bypassing
the delay but not the mask. `monitoring/decision.py` now level-triggers too: it
re-asserts an unresolved actionable change once its re-alert interval has elapsed
and coalesces a burst of successive changes to one subject, through a window on
`MonitorState`. So re-assertion-after-a-window is available on both paths rather
than only the cron one.

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

**The stall streak is engine state.** A watch whose verdict has been
byte-identical across N settled ticks is stuck, and the engine stops it. That
counter belongs in the persisted state the engine reads, not in a file that only
an instruction knows to maintain -- an advisory counter maintained by prose is
lost to exactly the long-running compaction it exists to survive. `MonitorState`
holds it as `stall_digest`, `stall_streak` and `stall_started_at`, and
`decide_monitor` folds them after the effect is chosen, because the verdict a
digest summarizes does not exist before then.

Four things the shape decides, each for a reason worth keeping:

- The digest is **derived** from the verdict on every tick, never persisted
  beside a second copy of it. What the record keeps is the digest of an EARLIER
  verdict, which nothing else holds, so the one-source-of-truth rule above
  survives one layer up. The **decision** is inside the digest, not only the
  entries, and that is what makes "with no progress" the same question as
  identity: a wake, a record, a retry, a new fingerprint or a moved head all
  change the digest, so a tick that progressed cannot match. What stays
  invisible is the woken agent's own uncommitted work, and no condition
  available at this layer can see that.
- A tick counts only when it **settled the subject and the engine then did
  nothing about it** -- a `NO_CHANGE`. `PENDING` is the domain's not-concluded
  class -- `checks_incomplete`, `checks_pending`, `review_threads_incomplete` and
  the rest -- so a pending tick never counts and a watch is never retired for
  being early. `PROVIDER_ERROR` never counts either, being no evidence about the
  subject and already carrying its own budget. **Every other tick zeroes the
  streak**, because it means the watch was working: a wake acted, a `RECORD_ONLY`
  held a change inside the coalescing floor, a `RETRY_PROVIDER` waited on
  incomplete evidence, a stop already ended it. Counting a deferral is the same
  error as counting a pending tick, and at the 15s minimum cadence a held change
  reaches twelve identical `RECORD_ONLY` verdicts 180s into a 240s window --
  retiring the watch before the window could release the wake it was folding.
  Zeroing on an UNSETTLED tick is what makes reading a clock in the trip safe: a
  streak left standing through a long pending stretch would let time alone satisfy
  the floor, and the next unsettled terminal tick -- a non-retryable provider
  error -- would be filed as a stall. The price is that a subject whose checks
  flap never accumulates a streak and is retired by its runtime budget instead: a
  later stop with an honest reason, which beats an earlier one with a false
  reason.
- The trip needs a **measured wall-clock floor** as well as the count, because
  the count answers the right question in the wrong unit on its own. Cadence is
  user-set from 15s to 86400s, and the streak's clock starts on its first counted
  tick, so twelve ticks is eleven intervals -- 3300s at the 300s default, 165s at
  the 15s minimum -- and 165s of an unchanged subject is a watch whose agent is
  still working. `stall_started_at` records when the streak
  opened and the trip reads `now - stall_started_at`, so nothing is translated
  from ticks into seconds. Storing a ceiling derived from `cadence_secs` would
  make the trip a pure integer comparison, at the price of a cached value derived
  from a mutable input with nothing invalidating it: a streak opened at the
  default would carry that ceiling into a 15s cadence and trip a quarter of the
  way into its floor. Any such translation breaks on
  a cadence change in one direction or the other, so there is none, and no
  invalidation rule to get wrong. Reading the clock reopens nothing: the hazard a
  snapshot was avoiding was a predicate an operator could make true between two
  folds. The clock is `time.time()`, a wall clock rather than a monotonic one, and
  neither direction reopens that hazard: backwards, the elapsed comparison goes
  negative and only delays a trip; forwards, a jump can satisfy the floor early but
  cannot manufacture the counted ticks the other half of the condition requires.
- Both thresholds are **bounded by numbers already in the module** rather than
  chosen freely, which is what lets a reader check them. The count must exceed
  the floor's tick equivalent at the default cadence, or it never binds, and stay
  inside the ticks a default watch gets before its runtime budget, or the stall
  can never fire: `6 < 12 < 48`. The floor must clear the coalescing window by a
  wide margin, or a folded burst looks like a stall, and stay well under the
  re-alert interval, because a re-alert wakes the subject and zeroes the streak,
  so a floor at or past it could never be reached: `240 << 1800 << 21600`.
- The stop records `verdict_stall`, distinct from `approval_stall` (a delivery
  failure) and from every `*_budget` reason (a spent bound), because a stop that
  reads like a convergence or like a cost is the cycle-cap anti-pattern under a
  new name. The engine owns that reason through `monitor_stall_reason`, the way
  it already owns `monitor_budget_reason` -- taking the tick's `now` as a value
  for the same reason that function does, so it cannot disagree with the fold that
  decided the stop. Both writers of `stopped_reason` otherwise copy the
  observation's own code and would file a stall as `checks_failed`.

Streak counters do exist and are about something else: `quiet_streak` with
`floor_ticks` counts consecutive quiet observations and the deliveries they
force, `consecutive_provider_errors` counts provider failures, and `irq.py`
carries its own consecutive-error backstop. None of them notices a watch that
keeps reaching the same conclusion.

#### The decision is split, and only half of it is pure

`decision.py` holds the **content** policy: did the subject change (its
fingerprint against `last_wake_fingerprint`), is the budget spent
(`monitor_budget_reason`), is this error retryable (`_provider_error_decision`
against `_RETRYABLE_PROVIDER_ERRORS`). It takes the clock as a value through its
`now` parameter and performs no IO at all.

**A skip is a third outcome, and it reaches three places.** A shared `github:api`
cooldown makes a probe return WITHOUT calling the API, and it borrows the shape of a
refusal to say so (`REASON_SHARED_COOLDOWN`, `is_unattempted_probe`). That is
neither a success nor a provider error: it is no evidence about the subject at all,
so it moves NEITHER counter — at `shadow.apply_monitor_probe` and at the production
counting site in `autonudge`. `_provider_error_decision` is the third place, and it
is the one a counter fix does not reach: it reads the same budget one tick into the
FUTURE (`consecutive_provider_errors + 1 >= max_provider_errors`), so a watch two
real errors into a budget of three would be retired by an unrelated scope's
cooldown, having made no call of its own. The prediction therefore refuses to spend
an unattempted probe, while `monitor_budget_reason` above it keeps stopping a watch
whose budget is genuinely gone — a cooldown must not become a way to outlive the
ceiling. The kind gate stays FIRST of the three, so an unattempted probe reporting a
non-retryable kind is still blocked.

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
| `alerted` | per-key alert timestamps -- the level-triggered state. Records that a wake was DECIDED, not that one was delivered: the persistence-only shadow path stamps a wake it deliberately refuses to deliver, so this is not delivery history and a report must not read it as such. The structured engine's `MonitorState.coalesce_alerted` mirrors it |
| `coalescing` | the open window: when it opened, which keys joined |
| `errors` | per-kind counts, so a retryable class stays bounded |
| `budgets_spent` | turns, tokens and provider errors already charged |
| `stall` | the verdict digest, its consecutive-match count, and when that streak opened |

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
These are code, or say in their own text where an implementation still diverges
-- deleting the instruction before the engine enforces the rule would leave it
enforced nowhere:

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
- A published aggregate is one signal in the worst-wins fold, never an override
  of the rows. It is read like any other row: a `PR Readiness` StatusContext with
  state FAILURE is a failing row and makes the verdict red, and one with state
  PENDING keeps the monitor waiting instead of concluding early. What it cannot
  do is subtract information -- a green aggregate does not clear an observed
  failing row, because the aggregate is identified by its context name, a display
  string any status publisher on the pull request can set, and a name anyone can
  write must not be able to remove a failure. The fold keeps the aggregate's
  failure and its pending while granting its green no authority over the rows.
  The problem aggregate authority was reaching for is real and still answered:
  green rows sitting under a still-pending aggregate must not conclude the round
  early. Worst-wins pending already answers it -- a pending aggregate is a
  pending row, so the monitor waits -- without letting a green one subtract a
  failure. Both current implementations now follow this rule: the structured
  provider reads the aggregate as an ordinary worst-wins row, and the skill's
  status tool was brought onto the same rule by
  [PR #10731](https://github.com/kirodotdev/KiroCrew/pull/10731), merged
  2026-09-14.
- A stale reviewer stamp is an entry (`stale:<name>`), not a paragraph.
- An un-dispositioned finding is an entry, so readiness cannot be declared over
  one.
- The stall streak is engine state, so a stuck watch stops itself. `MonitorState`
  carries `stall_digest`, `stall_streak` and `stall_started_at`; `decide_monitor`
  folds them and `monitor_stall_reason` names the stop `verdict_stall`.
- A retained stop is evidence, and an arming tool must not acknowledge an arm it
  cannot apply. Only a SYSTEM-imposed outcome is displaceable by a re-arm; a
  consumer-recorded one (`USER_STOP`, `SESSION_CLOSE`, a quarantined record, an
  outcome this version does not recognise) is preserved, and clearing it is an
  owner-only dashboard action because it destroys audit evidence.
  `monitoring.models.retained_outcome_blocks_rearm` is the one predicate that
  answers this, consumed both by `autonudge._stopped_row_is_replaceable` at the
  enforcement point and by the `mcp_tools.control` preflight that refuses in band
  before the model ends its turn. What that shared predicate buys is that the
  RULE cannot drift between the two sites; the preflight remains advisory, since
  it fails open on an unreadable record and the turn boundary is what enforces.
  Two copies of the rule is what lets the ack and the applier disagree on the
  classification itself, which is the false-acknowledgement class of bug.

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
| One fingerprint per subject | cannot say which of several simultaneous conditions changed, and cannot coalesce sibling conditions into one wake; it does carry temporal coalescing and re-assertion of the single fingerprint over time |
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

## The two arming tools read in the wrong order, and the names stay

`monitor_start` arms the in-session timer. `monitor_watch` arms the observation-gated
probe. Read cold, that is backwards: `start` is the generic primary verb, so the older
timer reads as the default way to arm a monitor and the newer, cheaper, zero-token
probe reads as a variant of it. A reader picking by name picks the expensive one.
`patrol` would say what the timer actually does -- a watch waits and reports when a
fact changes, a patrol walks the route every interval whether or not anything did --
and the shipped conductor prompts already use that word for it.

The names stay anyway, and the reason is worth more than the fix would have been.

**A published tool name is a key that other people's persisted records were written
against, and every one of those records is a decision that silently changes meaning
when the key changes.** Restrictions are the dangerous half: a persisted rule naming
a tool that no longer exists does not fail loudly, it stops matching. The capability
the operator switched off comes back on, and nothing at the call site says so.

Kiro Crew resolves tool restrictions at several name-keyed surfaces, at different
lifecycle stages, in different shapes:

| Site | Lifecycle stage | Shape | Reachable from code |
|---|---|---|---|
| `mcp_shared._resolve_tool_policy` | per call, cached per session | `ToolPolicy(excluded, unresolved)` from `managedToolPolicy.exclude` | yes |
| the same function's unresolved returns | before the exclude list is parsed | empty set plus the reason it could not be read; `tools/call` refuses, `tools/list` still lists | nothing to migrate; the refusal is audited per call |
| `acp/kas_agents.to_client_custom_agent` | startup projection, before the session exists | `excludedTools` list relayed to the agent host | yes |
| `acp/session_mcp.session_mcp_disabled_tools` | session projection: Claude `permissions.deny`, codex `rawInput.server`/`tool` | `(server, tool)` pairs, unioned from the agent spec AND the dashboard-written global `mcp.json` | yes |
| `agent._WORKER_MIRRORED_SHAPES` | derive-time copy | copies the persisted key | copies rather than resolves, so a rewrite here would alter a user's stored value |
| `GET /api/session-tool-policy` | on request | the raw persisted rule | deliberately raw, so an operator can see a stale spelling and re-key it |
| a hand-written block in an on-disk profile | the backend reads the file itself | unknown to this repo | **no** |

The last row is what settles it. `acp/kas_agents.py` states the boundary: a
hand-written block "is not ignored, just not Crew's to relay: it lives in the profile
on disk, which the backend reads itself when Crew is not injecting an agent over the
wire." No code here composes that file, so no migration can expand a retired name in
it and nothing can warn the operator holding one.

So the best achievable end state for renaming a published tool is a known silent
fail-open that cannot be closed -- not a step on the way to a complete job, but the
complete job's residue. A rename of a published name is therefore a policy migration
with a permanent remainder, not a legibility change, and it should be priced that way
before it is approved rather than discovered one surface at a time.

Weighed against that: the mispick this rename would prevent has not been observed.

What remains available, because none of it is name-keyed: the tool descriptions, this
spec, and the prompts that choose between the two. A caller reading `monitor_start`'s
description learns it is the timer without the name having to carry it.

### Which of the two is the default is an installation's choice

Both arming tools are reachable on a stock install and neither is gated. `GET
/api/monitors` answers its `enabled` field from whether the service object exists
(`handlers/autonudge.py`), not from a key, so there has never been a switch that
turns the structured engine on or off. What was unsettable is which of the two an
arming takes, and the reason follows from the section above: no code chooses, so
the choice is made by the model reading the two descriptions.

`monitoring.prefer_structured_arming` (boolean, default `false`) decides which
side has to justify itself. Off, the structured path is admissible only once the
caller has satisfied itself the objective is fully determined by typed provider
facts. On, a supported pull request is enough and the prompt loop becomes the
exception that needs its own reason, and `monitor_watch`'s own description says
so too. That is the whole extent of the key: it refuses neither tool, moves no
argument in either `inputSchema`, and cannot guarantee which path the agent then
arms -- the text is what it changes, and the text is what its tests measure.

The two positions are NOT two routes, and reading them as a swap of defaults is
the error to avoid: both send evidence the typed provider cannot observe --
comments, advisory review findings -- to the prompt loop. What moves is the
burden of proof. Off leans to the loop whenever the caller is unsure whether the
objective is fully typed-decidable, because that judgement is the precondition;
on, being a supported pull request is the precondition and the loop needs a
positive reason. An installation that wants the cheap path for a few days is
asking for exactly that shift, which is why the key is worth a row in the schema
even though it changes no code path.

It is read in `mcp_tools/control.py::schemas()`, which is the only place it is
read, and read afresh on every build: `mcp_tools.build_tool_list` rebuilds
descriptors per call rather than caching them, so a Settings write needs no
gateway restart. It does not reach a session already open, because kiro-cli
caches a session's tool list for that session's life -- the same limitation
`mcp_tools/browser.py` records for `dashboard.use_builtin_browser`. A config read
that raises resolves to the off position, since off is what ships.

The key deliberately does not make `monitor_start` REFUSE a target the structured
engine supports. `monitor_start` takes a free-text message and no typed target,
so a refusal would have to guess from prose which armings were structured-capable;
and `goal-conductor` and `pipeline-conductor` patrol their own session with it as
their primary use, which a prose-matching refusal would break. A preference that
cannot misfire is worth more here than an enforcement that can.

The descriptor is not the only prose that states the choice, and the key moves
only the descriptor. `config/prompt.md` carries the same rule in its own words
("Prefer bounded `monitor_watch` when typed provider facts decide the whole
objective"), and that sentence is true in both positions, so it is left alone
rather than made flag-aware. The reason is reach, not effort: a prompt file is one
of many agent prompts, while the descriptor is assembled for every session
whatever prompt it runs, so the descriptor is the only copy a single read can
move for all of them. An installation that turns the key on therefore gets the
default restated on the tool list, not rewritten in every prompt.

#### Two costs of making the structured path the default

Both are properties of the engine rather than of the key, and both are what an
operator is actually buying, so the key's help text names them.

**Half of a review-ready objective is invisible to the typed provider.** A
structured observation carries lifecycle, checks, mergeability, review decision
and review-thread counts, and nothing else; `docs/architecture/mcp.md` states the
same boundary from the tool's side ("requests that need comments or advisory
findings route directly to the finite legacy tool whose agent turn can inspect
them"). On this repository that is not a corner case: a pull request reaches
`readiness: passed` only once every non-PASS whole-design verdict carries a
disposition, and those verdicts live in comment bodies. A green typed board and an
unanswered advisory finding are indistinguishable to a probe, which is why the
prompt loop keeps `gate=false` for that evidence in both positions of this key.

**An armed structured monitor is not freely swappable, though the key is.**
Flipping the key back restores the previous wording on the next tool-list build
and needs nothing else. An already-armed monitor is different:
`monitor_stop` records `MonitorOutcome.USER_STOP`
(`autonudge._apply_monitor_user_stop`), and `_stopped_row_is_replaceable` admits
only `BUDGET`, `SUCCESS`, `BLOCKED` and `TARGET_UNAVAILABLE` -- the
system-imposed outcomes. A consumer-recorded stop is retained evidence, and an
unknown outcome fails closed the same way, so the next arm on that session is
refused with "its owner must clear it first from the dashboard's goal popover".
The consequence for an operator who turns this key on and then wants one session
back on the prompt loop: an agent cannot make that swap, and the remedy is the
owner's clear or restart action, never a retry.
