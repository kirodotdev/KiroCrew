---
title: Token-efficient monitors — probe first, wake on change
status: draft
author: kseam
created: 2026-08-22
last-audited: 2026-08-22
audited-at: 6d3e30bbbd
doc-pr:
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# Token-efficient monitors — probe first, wake on change

## Summary

Replace babysit's repeated full-agent turns with a durable, typed monitor that
checks external state before deciding whether the owning session needs to run.
The agent calls one session-scoped MCP tool to create the monitor. Kiro Crew then
performs deterministic, inexpensive probes on schedule and wakes the session only
when the observation is new and actionable.

The first monitor watches a GitHub pull request until it is review-ready. It
tracks checks, reviews, mergeability, and head revision without asking the model
to rediscover unchanged state. The existing AutoNudge timer remains the durable
scheduling primitive; a monitor controller adds domain state, fingerprints,
budgets, terminal outcomes, and completed-turn accounting.

This work lands as a stack. PR 0 is this RFC. PRs 1–6 introduce the durable model,
correct accounting, a shadow-mode GitHub probe, the agent-facing tool, truthful
dashboard controls, and finally removal of prompt-owned orchestration from the
babysit skill.

## Decision

Kiro Crew will provide a structured, session-scoped monitor capability:

```text
monitor_watch(
  kind="github_pull_request",
  target="https://github.com/owner/repo/pull/123",
  objective="review_ready",
  interval_secs=300,
  max_runtime_secs=14400,
  max_agent_turns=8,
  max_tokens=250000,
)
```

The agent/session calls this tool once. The server owns subsequent polling and
decision-making. An unchanged observation costs zero model turns. A terminal
observation stops and records the outcome without a model turn. A new actionable
observation wakes the owning session once with a compact summary. The same
fingerprint never wakes twice.

The initial API also includes `monitor_update` and `monitor_stop`.
`autonudge_stop` remains as a compatibility alias while legacy goal loops exist.

## Motivation

### Babysit is currently prompt policy over a generic timer

The existing babysit skill creates an AutoNudge loop whose message asks a future
model turn to inspect state, infer progress, act, and decide whether to stop. The
runtime persists scheduling data, but it has no target-domain state, success
predicate, progress fingerprint, blocker classification, or checkpoint. The skill
therefore asks the model to maintain a `.babysit-key-*` sidecar file even though
the scheduler never reads that file.

This makes completion voluntary. A future model turn must recognize the exit
condition and call the stop tool. The shipped skill records four observed loops:
all four stopped at their cap, and none recorded an agent stop reason. That is a
strong signal that the control loop is asking the model to do work the runtime
should own.

### Token use is structural, not merely a prompt-quality problem

Every AutoNudge cycle appends a full turn to the same session. The model repeatedly
receives conversation context, invokes tools to rediscover remote state, and emits
another answer even when nothing changed. Compaction can reduce the size of old
context, but it cannot make unchanged polling free.

The desired invariant is stricter:

> If the monitored external state has not materially changed, Kiro Crew performs
> no model turn.

That requires the state comparison to happen before the model is invoked.

### Delivery and budget semantics differ by surface

Dashboard delivery currently counts a cycle after dispatching an asynchronous
turn. Slack and Discord count after the inline turn completes. Runtime-budget
checks therefore describe dispatch on one surface and completed work on the
others. A monitor needs one accounting boundary across every surface: a wake is
charged only after the resulting turn completes and usage is known.

### Terminal evidence is incomplete

Ordinary stop removes the loop record and discards its free-form reason. Caps and
budgets leave an inactive record. Session close, sentinel removal, user stop, and
unreachable destinations remove records. A successful monitor must retain enough
evidence to answer what it watched, why it stopped, how much work it caused, and
whether the objective was satisfied.

### The dashboard exposes a different safety posture

The MCP path defaults to a five-minute cadence and 24 cycles. The dashboard
defaults to a one-minute cadence with unlimited cycles and exposes no runtime
budget. The dashboard can receive an inactive record but does not model its stop
reason; saving that record revives it. A first-class monitor must make bounds and
outcomes explicit rather than presenting every inactive loop as an editable
schedule.

## Goals

- Make unchanged observations consume zero model turns.
- Let the agent/session create, update, inspect, and stop a monitor through
  stateless MCP tools.
- Keep per-session identity resolution and authorization at the existing gateway
  boundary.
- Persist enough typed state to resume safely after gateway restart.
- Deduplicate wakes by canonical observation fingerprint.
- Stop terminal objectives and deterministic blockers without invoking the model.
- Enforce runtime, agent-turn, token, and provider-error budgets.
- Account for a wake at completed-turn time on every delivery surface.
- Retain durable terminal outcomes for UI, metrics, and debugging.
- Preserve legacy AutoNudge behavior during migration.
- Land in reviewable, independently useful stacked PRs.

## Non-goals

- Replacing cron or the generic AutoNudge timer.
- Building a general workflow language.
- Letting arbitrary shell commands act as probes.
- Supporting every forge or arbitrary website in the first release.
- Running action turns in a new hidden session in the first release.
- Automatically fixing every PR condition without an agent turn.
- Removing legacy goal loops before the structured monitor path is proven.
- Treating model compaction as a substitute for pre-model deduplication.

## Design

### 1. Control plane: a stateless MCP tool called by the agent/session

`monitor_watch` is an MCP tool, not a magic natural-language mode. Like other
session-mutating tools, it resolves the current session with the strict resolver
and emits a stateless directive. The dashboard chat runner authenticates and
applies that directive to the authoritative session binding. The MCP process
holds no caller state.

The create request contains:

- monitor kind and version;
- target and objective;
- cadence and budgets;
- optional compact wake instructions;
- caller session identity supplied by the trusted directive envelope.

The tool acknowledgement means “the request was accepted for application,” not
“the monitor is durably armed.” The applied result and subsequent GET/list state
are authoritative. The UI must use that language as well.

`monitor_update` changes cadence, budgets, or wake instructions without resetting
the observation fingerprint unless the target or objective changes. Every durable
budget edit carries only its explicitly supplied fields and merges under the
monitor service lock, preserving concurrent edits to other limits. Every durable
target/objective edit advances a configuration generation; a probe applies only
against the generation and target it captured. Identity edits are refused while
an action wake is in flight so completion correlation and accounting remain valid.
Creating a replacement monitor is refused under the same condition; the existing
monitor ID remains authoritative until its correlated completion is charged.
`monitor_stop` records an explicit terminal outcome instead of deleting evidence.
When the in-flight action itself stops its monitor before its raw completion
event, the terminal record retains that wake's correlation until completion
charges and clears it. Missing or duplicate completion cannot re-arm, redispatch,
or double-charge the terminal record.

### 2. Durable model: typed state beside legacy loops

AutoNudge remains the timer and atomic persistence owner. `NudgeLoop` gains an
optional versioned monitor payload. Absence of that payload means legacy prompt
loop and preserves existing behavior.

The monitor payload contains at least:

```text
kind, version, target, objective
last_observation, last_fingerprint, last_observed_at
config_generation, last_wake_fingerprint, wake_in_flight
wake_delivery, completion_evidence_deadline
wake_count, agent_turns, input_tokens, output_tokens
consecutive_provider_errors, next_probe_at
outcome, stopped_reason, stopped_at
```

The exact serialized names are implementation details, but the following
invariants are not:

1. A monitor is either active, terminal, or legacy; terminal is not deletion.
2. A target/objective change clears the comparison baseline.
3. A cadence-only change preserves the comparison baseline.
4. Only a completed action turn consumes an agent-turn or token budget.
5. Restart recovery cannot turn an acknowledged fingerprint into a duplicate wake.
6. Unknown future monitor versions fail closed and remain inspectable.

No eager one-way migration rewrites existing `autonudge.json` records. Legacy
records deserialize without a monitor payload, and new fields use safe defaults.

### 3. Observation: small, canonical, and domain-owned

The first probe supports `github_pull_request` with objective `review_ready`.
It reads the minimum remote state necessary to classify the pull request:

- repository and pull-request number;
- current head revision;
- open, closed, or merged state;
- draft state;
- required/check rollup;
- review decision and unresolved blocking review state;
- mergeability when the provider supplies it;
- provider error category.

The probe returns a canonical observation rather than raw provider output. A
stable serialization is hashed to form the fingerprint. Volatile values such as
request IDs, timestamps, ordering differences, URLs with transient parameters,
and log text are excluded. A changed head revision necessarily changes the
fingerprint and resets state that is revision-specific.
When a GitHub response contains both known actionable blockers and unsettled
pending/unknown facts, the actionable classification wins. Its deduplication
fingerprint retains the known failed checks, review blockers, and merge blockers
while excluding unrelated pending/unknown check churn; the full canonical fact
snapshot is still persisted for inspection.

Raw logs are not embedded in the persisted observation or wake message. The
action turn fetches them on demand only when the compact observation identifies a
specific failing check.

### 4. Decision: a pure policy before delivery

The controller evaluates a canonical observation with a pure decision function.
Its output is one of:

```text
NO_CHANGE
RECORD_ONLY
WAKE_ACTIONABLE
STOP_SUCCESS
STOP_BLOCKED
RETRY_PROVIDER
STOP_BUDGET
```

Representative rules:

- Same fingerprint as the last observation: `NO_CHANGE`.
- Pull request merged or closed: terminal without a model turn.
- Objective satisfied: `STOP_SUCCESS` without a model turn.
- New failing check, blocking review, conflict, or changed head revision:
  `WAKE_ACTIONABLE` once for that fingerprint.
- Pending checks with no other material change: `RECORD_ONLY`.
- Rate limit, transient transport failure, or provider outage:
  `RETRY_PROVIDER` with bounded exponential backoff and no model turn.
- Authentication, authorization, missing repository, or repeated provider
  failures: `STOP_BLOCKED` with a durable reason and no model turn.
- Runtime, turn, or token budget reached: `STOP_BUDGET`.

The decision function has no network or persistence access. Table-driven tests
pin every transition and its budget effect.

### 5. Token discipline

The controller enforces these product invariants:

- unchanged remote state: zero model turns;
- repeated provider failures: zero model turns;
- success or deterministic terminal state: zero model turns;
- one actionable fingerprint: at most one model turn;
- wake payload: at most 4,096 characters;
- raw logs and large diffs: fetched only by the action turn when needed;
- default cadence: 300 seconds;
- default wall-clock budget: 14,400 seconds;
- default agent-turn budget: 8;
- default aggregate token budget: 250,000 input plus output tokens;
- default consecutive provider-error limit: 3.

First-class monitors do not accept an unlimited value for these budgets in the
initial release. Legacy loop values retain their existing `0` semantics while the
compatibility path exists.

The compact wake includes the monitor identity, objective, new fingerprint,
classification, head revision, changed facts, and one suggested next action. It
does not replay the full polling history. The session transcript remains the
working context for an action turn; the durable monitor record remains the source
of truth for polling state.

### 6. Delivery: count completed work, not dispatch

Probe execution never enters the owning chat session. `WAKE_ACTIONABLE` requests
one normal turn on the existing session. The monitor marks that fingerprint as
in-flight before dispatch so another timer cannot enqueue a duplicate.
Probe decisions are staged and fsynced before changing the live record or timer,
so persistence failure cannot leave an actionable claim only in memory. The
claim is revalidated both before transport handoff and at each surface's final,
yield-free turn-start boundary. Dashboard performs that final check in its runner
after pre-turn setup, then crosses SessionManager's synchronous shutdown gate
before appending the wake or reporting `DISPATCHED`; shutdown refusal reports
`BUSY` without changing chat history. Slack and Discord run the same shutdown
gate before accepting the completion correlation, so a lease claimed before
teardown cannot open a turn after the shutdown drain snapshot.
Every other structured transition uses the same staged replacement boundary:
configuration changes, close retirement and rollback, claims, handoff results,
completion accounting, and terminal recovery reach live readers and timers only
after the replacement snapshot is durable. A close rollback also rechecks the
restored slot generation under the service lock before it can reactivate the
monitor.

Every delivery surface reports a completion result to the controller containing:

- monitor and fingerprint identity;
- success, failure, cancellation, or approval-stall disposition;
- input and output token usage when available;
- completion timestamp.

The controller charges budgets and clears in-flight state only from this callback.
Dashboard, Slack, and Discord therefore share the same completed-turn boundary.
Before completion, every surface reports one typed handoff result: `DISPATCHED`
means the action was accepted and starts a durable, bounded evidence deadline;
`BUSY` durably rearms the already-claimed wake for a short retry without another
probe or model turn, but rechecks runtime before redispatch; `UNAVAILABLE` is
terminal. Dashboard reports `DISPATCHED` only after acquiring its background
permit, revalidating the claim, and crossing the shutdown gate; a permit timeout
or shutdown refusal is `BUSY`. If dispatch fails before a turn starts, no
agent-turn or token budget is charged. Stream exhaustion is dispatch, not
completion. If the evidence deadline expires without a raw completion event,
the monitor atomically retains `completion_evidence_unavailable`, clears the
claim, and fails closed rather than issuing a duplicate wake. Late handoff
callbacks never replace an outcome that became terminal during delivery.
Restart resumes a persisted `BUSY` claim at its existing retry deadline. Cadence
edits affect only
future probes: they cannot replace an in-flight BUSY retry or accepted-dispatch
evidence deadline, re-arm its timer, or postpone runtime enforcement.
`wake_count` increments once when an actionable wake is first accepted as
`DISPATCHED` (or when its raw completion wins the handoff race). `BUSY` retries,
restart recovery, duplicate handoff reports, and `UNAVAILABLE` do not increment
it.
Discord makes this handoff at its dispatcher concurrency boundary: a monitor wake
that loses the session race is `BUSY` and is neither steered nor queued, a
pre-turn refusal is `UNAVAILABLE`, and `DISPATCHED` is possible only after the
started turn owns the correlated completion hook. Its outer turn timeout remains
bounded; after hook acceptance a timeout preserves `DISPATCHED` and lets the
completion-evidence deadline recover the correlated turn.

The initial implementation uses the owning session for action turns. A dedicated
monitor-action session may be evaluated later if transcript growth remains a
material cost after pre-model deduplication.

### 7. Outcomes and observability

Terminal monitors remain in the persisted registry until the normal retention
policy removes them. An outcome records:

- success, blocked, budget, user stop, session close, or target unavailable;
- stable machine-readable reason code;
- concise human-readable detail;
- last observation and fingerprint;
- probe count, wake count, completed agent turns, and token totals;
- created, last-observed, and stopped timestamps.

Metrics distinguish probes from model turns. The minimum useful counters are:

- probe attempts and provider errors by monitor kind;
- observations classified by decision;
- deduplicated no-change cycles;
- action turns and token totals;
- terminal outcomes and reasons;
- dispatch-to-completion latency.

These fields make the central efficiency claim measurable: probes may grow while
model turns remain proportional to material changes.

### 8. Dashboard: separate monitors from legacy goal loops

The dashboard presents structured monitors and legacy goal loops as different
concepts even while both use AutoNudge internally.

For a structured monitor it shows target, objective, next probe, bounds, latest
classification, usage, and terminal reason. Terminal records are read-only; the
user explicitly restarts a monitor rather than reviving it through a generic Save
action. The create form uses the same safe defaults and bounds as the MCP tool.

Legacy “Set a goal” remains available during rollout with an explicit legacy
label. Browser creation is create-only at the durable service boundary, so a
stale empty read cannot replace an automation concurrently armed in another tab.
It no longer implies that an inactive capped loop completed successfully.

## Migration plan

### PR 0 — RFC: specify probe-first monitors

Suggested title: `docs: specify token-efficient monitors`

Scope:

- Add this RFC and index it.
- Record the cross-surface invariants, tool boundary, persistence strategy,
  budgets, and stacked implementation plan.
- Make no runtime changes.

Verification:

- Run the documentation lint gate.
- Review every proposed state transition against the current AutoNudge lifecycle.

Exit criteria:

- The stack has an agreed architectural boundary and measurable token invariants.
- Open questions that block PR 1 are resolved or explicitly deferred.

### PR 1 — Durable monitor model and pure decision engine

Suggested title: `feat: add durable monitor decisions`

Primary areas:

- `src/kiro_crew/monitoring/` for typed observations, outcomes, and pure policy;
- `src/kiro_crew/autonudge.py` for optional versioned monitor persistence;
- `test/test_monitor_decision.py` and persistence compatibility tests;
- the AutoNudge system specification.

Test-first sequence:

1. Add table tests for every decision result, fingerprint deduplication, head
   revision changes, provider-error thresholds, and budget precedence.
2. Add round-trip tests proving legacy records remain byte-compatible in behavior
   and future versions fail closed.
3. Implement the minimum data model and pure evaluator that pass those tests.

This PR does not call GitHub and does not wake a session.

Exit criteria:

- Decision logic is deterministic and independent of transport or persistence.
- A legacy AutoNudge registry loads unchanged.
- A monitor record survives restart with its fingerprint, budgets, and outcome.

### PR 2 — Completed-turn accounting and usage budgets

Suggested title: `refactor: account for completed monitor turns`

Primary areas:

- AutoNudge delivery callbacks;
- dashboard, Slack, and Discord completion paths;
- usage extraction from the ACP turn result;
- cross-surface contract tests;
- messaging, dashboard, and AutoNudge specifications.

Test-first sequence:

1. Add a shared contract test demonstrating that dispatch alone charges nothing.
2. Require all three surfaces to charge exactly once after completion.
3. Cover cancellation, dispatch failure, approval stall, restart while in-flight,
   missing usage, and runtime/turn/token precedence.
4. Refactor delivery around one completion result contract.

Legacy cycle-count behavior remains available behind the legacy path; structured
monitors use completed-turn accounting.

Exit criteria:

- Dashboard, Slack, and Discord have identical monitor accounting semantics.
- Duplicate or missing completion callbacks cannot create an unbounded wake loop.
- Token and turn budgets are enforced from observed usage.

### PR 3 — GitHub pull-request probe in shadow mode

Suggested title: `feat: add github pull request monitor probe`

Primary areas:

- a GitHub provider adapter behind the monitor probe interface;
- canonical observation and fingerprint tests with recorded provider shapes;
- authorization, redaction, retry, and rate-limit handling;
- metrics and inspectable shadow results;
- monitor and security specifications.

Shadow mode schedules probes and records decisions but cannot wake a model. It is
used to validate canonicalization, rate-limit behavior, and false-action rates on
real pull requests without spending agent tokens.

Test-first sequence:

1. Pin canonical observations for draft, pending, passing, failing, reviewed,
   conflicting, merged, closed, inaccessible, and rate-limited pull requests.
2. Prove reordered or volatile provider fields do not change the fingerprint.
3. Prove a new head revision does change the fingerprint.
4. Add integration tests against a fake provider boundary; network tests remain
   outside the normal unit suite.

Exit criteria:

- An unchanged pull request can be probed repeatedly with zero model turns.
- Shadow decisions are stable enough to compare against human expectations.
- Provider failures back off and terminate according to the RFC.

### PR 4 — Structured MCP tools and probe-gated wakes

Suggested title: `feat: expose session monitors to agents`

Primary areas:

- `monitor_watch`, `monitor_update`, `monitor_stop`, and inspect/list tools;
- stateless directive validation and authoritative application;
- monitor controller wiring to the existing AutoNudge scheduler;
- compact injected-message envelope;
- end-to-end tests from tool call through completed action turn;
- MCP, session, injected-message, and AutoNudge specifications.

Test-first sequence:

1. Add strict-session and native-subagent rejection tests matching existing
   session-mutating tool rules.
2. Add authorization tests for dashboard and supported channel bindings.
3. Prove no-change, retry, and terminal decisions never dispatch a turn.
4. Prove one actionable fingerprint dispatches once across restart and concurrent
   timer callbacks.
5. Prove BUSY retries only the claimed wake, an accepted dispatch has a bounded
   completion-evidence deadline, and unavailable delivery is terminal.
6. Prove stale probe generations cannot apply and identity edits cannot break an
   in-flight action's completion correlation.
7. Prove stop records a durable outcome and legacy mutation routes cannot bypass
   structured authorization or scheduling invariants.

Exit criteria:

- The agent can create the monitor with one tool call.
- Server-side probes gate every subsequent model wake.
- The public wake envelope is bounded, documented, and treated as automation.
- Provider-controlled check identities never enter the wake prompt; it carries
  status counts only.

### PR 5 — Truthful dashboard monitor experience

Suggested title: `feat: show bounded monitors in the dashboard`

Primary areas:

- separate TypeScript models for structured monitors and legacy loops;
- create, inspect, stop, and explicit restart controls;
- status, outcome, reason, usage, and next-probe presentation;
- aligned MCP/REST defaults and validation;
- frontend unit and browser-level tests;
- dashboard and i18n documentation.

Test-first sequence:

1. Pin inactive terminal records as read-only and prevent Save from reviving them.
2. Pin safe defaults and reject unlimited structured monitor budgets.
3. Cover arm-pending, active, backing-off, action-running, success, blocked, and
   budget-stopped states.
4. Verify sidebar and detail views derive status from the same normalized state.

Exit criteria:

- The UI never labels unresolved expiration as completion.
- The UI exposes why a monitor stopped and what it consumed.
- Dashboard-created monitors have the same safety posture as agent-created ones.

### PR 6 — Make babysit a thin compatibility skill

Suggested title: `refactor: route babysit through structured monitors`

Primary areas:

- replace the pull-request babysit recipe with `monitor_watch` instructions;
- remove the prompt-owned sidecar progress protocol;
- retain legacy guidance only for unsupported targets, clearly marked as costly;
- add product documentation and migration notes;
- evaluate legacy goal-loop deprecation from collected metrics.

Verification:

- Run scripted skill scenarios for create, actionable change, no change, success,
  blocked provider, budget stop, and user stop.
- Compare model turns and tokens against the current babysit baseline.

Exit criteria:

- The supported pull-request path contains no repeated polling turns.
- Completion is controller-owned rather than dependent on the model remembering to
  stop itself.
- Legacy prompt loops remain functional for unsupported use cases until a separate
  removal decision is approved.

## Backward compatibility and rollout

- Existing AutoNudge JSON records remain valid and keep current semantics.
- Existing REST and MCP goal-loop endpoints remain during the stack.
- `autonudge_stop` remains an alias for legacy callers; structured callers use
  `monitor_stop` so the intent and retained outcome are explicit.
- New serialized fields are optional and versioned. Older records receive no
  implicit target or objective.
- Structured monitors are initially limited to GitHub pull requests and the
  `review_ready` objective.
- PR 3 is shadow-only. PR 4 enables action wakes after shadow metrics and test
  fixtures demonstrate stable classification.
- The dashboard distinguishes both record types before the babysit skill defaults
  to the new path.
- Removal of legacy goal loops is outside this RFC and requires separate evidence
  and approval.

## Security and privacy

- MCP tools remain stateless and resolve the mutating session strictly.
- The gateway applies existing ownership, routability, channel, and native-subagent
  restrictions before creating or changing a monitor.
- GitHub credentials stay behind the provider boundary and are never persisted in
  monitor state or injected into the transcript.
- Targets are normalized and validated; the first implementation accepts only
  GitHub pull-request identities, not arbitrary URLs or commands.
- Provider output passes through existing secret redaction before persistence,
  logging, or delivery.
- Canonical observations use an allowlist of small fields. Raw logs, diffs, review
  bodies, and comments are not persisted by default.
- Error reasons exposed to the agent or UI use stable codes and sanitized detail.
- Background action turns retain existing governance and approval behavior. A
  monitor does not grant new tool authority.
- Terminal retention must follow the existing local data and deletion policy; it
  must not become an indefinite archive of provider content.

## Alternatives considered

### Improve only the babysit prompt

A better prompt can reduce mistakes, but an unchanged cycle still invokes the
model and grows the transcript. It cannot satisfy the zero-turn invariant.

### Use a cheaper model for every poll

This lowers price but preserves repeated model calls, voluntary completion, and
context growth. It is a fallback for unsupported targets, not the architecture.

### Model the monitor as a cron job

Cron already provides schedules, but merging it with session-bound monitoring
would blur ownership, completion callbacks, and interactive action turns. Reusing
AutoNudge's binding-aware timer is a smaller migration.

### Build the feature as a dynamic workflow

The workflow engine can represent long-running state, but the first requirement is
a narrow probe/compare/wake loop with strong deduplication. Introducing workflow
authoring and recovery semantics would expand the first release without improving
the token invariant.

### Run a dedicated hidden agent session

This can cap transcript growth, but it still spends a turn on unchanged state
unless a deterministic probe comes first. Same-session action turns preserve user
context and are simpler for the initial release. Dedicated sessions remain a
possible later optimization.

### Allow arbitrary commands as probes

Commands would make the feature generic quickly, but they create a background
execution and credential boundary that is much larger than a typed provider
adapter. The initial monitor is deliberately domain-specific.

### Replace AutoNudge entirely

The existing service already owns durable cadence, recovery, binding-aware
delivery, and race handling. The problem is its missing domain controller and
completion contract, not its timer.

## Open questions

These do not block PR 1 unless an implementation discovery changes the stated
invariants:

1. Which existing ACP result field is the most reliable cross-surface source for
   input and output token usage? PR 2 must define behavior when it is absent.
2. What terminal-record retention window best balances debugging value and local
   data minimization? The record must outlive immediate UI inspection, but this RFC
   does not require indefinite retention.
3. Should a review-ready objective require mergeability to be definitively clean,
   or may an unknown mergeability value be treated as pending? PR 3 fixtures must
   choose one conservative rule.
4. After the pull-request path is proven, should monitor actions move to a bounded
   companion session to further reduce transcript growth? Metrics from PRs 4–6
   should inform that decision.
5. Which monitor kinds should follow next? Candidate kinds must have a small,
   deterministic observation and a terminal predicate; general web polling is not
   implied.

## Provenance

This RFC was audited against Kiro Crew commit `6d3e30bbbd`. The current behavior
is distributed across the babysit skill, AutoNudge service, MCP control tools,
session directive applier, dashboard and channel delivery paths, and dashboard
goal-loop components. The implementation PRs must update the relevant system
specifications in the same commit as each behavioral change.
