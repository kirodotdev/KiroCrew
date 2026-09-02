---
title: Durable Run Coordinator — typed lifecycle, idempotent commands, and recoverable delivery
status: in-progress
revision: v1
author: Kyle Seaman, with Codex
created: 2026-08-22
last-audited: 2026-08-22
audited-at: 09b58e9b4
doc-pr:
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---
# RFC: Durable Run Coordinator — typed lifecycle, idempotent commands, and recoverable delivery

- Status: implemented locally — PRs 1–7 are prepared as a stack. Keyed
  execution carries a renewable run fence through `starting`, `running`, and
  atomic terminal/outbox commit; fenced delivery retries reuse one stable event
  identity. Restart recovery imports legacy folders read-only, takes over only
  expired leases, reports uncertain work as interrupted, and never replays it.
- Author: Kyle Seaman, with Codex
- Created: 2026-08-22
- Audited against: PR 1 commit `09b58e9b4`, PR 2 commit `ffe2b0f76`, PR 3 commit
  `6db805ec2`, PR 4 commit `458472368`, PR 5 commit `ee198e741`, PR 6 commit
  `391ccd202`, and local branch `run-coordinator-recovery` for PR 7
- Related: `docs/system-specs/modules/subagent.md`,
  `docs/system-specs/modules/session.md`, and
  `docs/request-for-change/rfc-orchestrator-chat-sessions.md`
- Detailed implementation plan:
  `docs/superpowers/plans/2026-08-22-durable-run-coordinator.md`

## 1. Summary

Introduce a local durable **Run Coordinator** between subagent callers and the
existing in-process executor. The coordinator owns typed run state, idempotent
commands, lease/fencing metadata, and a transactional delivery outbox in SQLite.
The existing `SubagentManager` remains the compatibility facade and local
executor while its scheduling and terminal-finalization responsibilities move
behind smaller lifecycle boundaries.

This is an evolution of Kiro Crew's current single-node design, not a distributed
scheduler rewrite. SQLite is the first coordinator implementation. It gives one
gateway process a canonical execution ledger and an explicit ownership protocol;
the interface leaves room for another durable backend later without requiring
distributed operation in this stack.

The migration keeps `state.json`, `tombstone.json`, and `result.txt` working while
the coordinator initially shadow-writes and checks parity. Authority moves one
operation at a time. `result.txt` remains the canonical full output artifact even
after the metadata mirrors can eventually be retired.

## 2. Motivation and current state

Verified at `c4f253891` on 2026-08-22.

### 2.1 Strong lifecycle invariants exist, but they are concentrated

`SubagentManager` begins at `src/kiro_crew/subagent.py:1344` and the module is
6,415 lines. Its constructor owns process execution, approval callbacks,
in-memory run/task registries, the admission queue, batch accounting, terminal
report tasks, follow-up watchers, conversation retention, and the periodic
reaper (`subagent.py:1347-1478`). The public `spawn()` path then handles identity,
admission, approval, queueing, persistence, event publication, and task creation
(`subagent.py`: `spawn`, `_announce_rejection`, `_drain_queue`,
`_spawn_with_approval`, and `_log_spawned`).

The lifecycle discipline inside that module is valuable and must be preserved.
In particular, terminal reporting and slot release are independently claimed by
`_claim_finalize()` and `_release_slot()` (`subagent.py`, both methods of
`SubagentManager`). The system spec documents four distinct terminal guards and why
collapsing them previously caused duplicate delivery, lost outcomes, or leaked
slots (`docs/system-specs/modules/subagent.md:175-190`). The problem is not the
existence of those invariants; it is that their state machine is distributed
across one very broad class and tested mostly through its private surface.

### 2.2 Persistence records evidence, not a canonical execution ledger

Every run currently gets a folder containing `state.json`, `result.txt`, and,
when terminal, `tombstone.json` (`src/kiro_crew/subagent_persistence.py:1-7`).
Folder creation writes a running snapshot (`subagent_persistence.py:88-125`),
updates rewrite that JSON (`132-147`), output appends to `result.txt`
(`153-163`), and terminal classification writes a tombstone
(`180-218`). Startup recovery scans non-tombstoned directories and infers what
happened from PID liveness and result presence
(`docs/system-specs/modules/subagent.md:447-490`).

That recovery is pragmatic, but no durable record atomically answers all of
these questions:

- Was a request accepted, rejected, queued, or dispatched?
- Has this exact command already been applied?
- Which gateway incarnation owns the attempt?
- Is the terminal outcome durable but its parent delivery still pending?
- May recovery retry delivery without retrying execution?

The current files remain excellent human-readable recovery artifacts. They are
not a transactional command, execution, and delivery ledger.

### 2.3 The submission boundary has an uncertainty seam

The `spawn_run` MCP tool submits wave members through separate HTTP requests
(`src/kiro_crew/mcp_tools/spawn.py:484-598`). A response can fail after the
gateway accepted the request, so the caller and gateway maintain extra
submission accounting and lost-submission reconciliation. The manager exposes
`record_lost_submission()` and a stuck-wave reaper `_sweep_stuck_waves()`
(`subagent.py`), while the dashboard handler reconciles the roster
against accepted IDs (`src/kiro_crew/dashboard/handlers/messaging.py:90-202`).

The existing preassigned run ID is an important foundation: `spawn()` assigns
identity before every exit path and preserves it through queueing
(`subagent.py`, the `_preassigned_id` parameter of `spawn()`). What is
missing is a durable idempotency record that
makes a repeated submission with the same command key return the same accepted
decision rather than opening a reconciliation side protocol.

### 2.4 Completion and delivery are not one durable transaction

The run path carefully shields terminal reporting, waits for teardown, and
re-admits a cancelled report to orphan recovery. This prevents several local
races, but completion state and the requirement to deliver it are represented
by separate in-memory flags plus file-marker ordering. `settle_queued_delivery()`
must wait for teardown before writing a delivered tombstone
(`subagent.py`) because that tombstone also suppresses restart
reconciliation.

A transactional outbox makes the intended contract direct: committing a
terminal outcome also commits one stable delivery event. Delivery can then be
retried independently and marked complete only after the destination accepts
that event.

## 3. Goals

1. Give every accepted run and lifecycle command a typed, canonical durable
   record.
2. Preserve the current exactly-once local terminal arbitration and slot
   accounting while extracting them behind a small public state machine.
3. Make duplicate or uncertain spawn/continue/steer/cancel submissions safe
   through stable idempotency keys.
4. Commit terminal outcome and pending delivery atomically, then provide
   at-least-once delivery without repeating uncertain execution.
5. Make gateway ownership explicit with leases and fencing, even while only one
   gateway is supported.
6. Keep SQLite and legacy disk I/O off the asyncio event loop.
7. Roll out additively, with parity checks and a reversible authority switch at
   every phase.
8. Preserve current MCP, HTTP, websocket, parent-injection, result-retention,
   approval, governance, and session behavior throughout the stack.

## 4. Non-goals

- Horizontal workers, multi-host scheduling, leader election, or a supported
  high-availability deployment.
- Moving full result text or ACP transcripts into SQLite. `result.txt` remains
  the canonical full-output artifact.
- Replacing `SessionManager`, TaskRunner, workflows, cron, or Crew Mode with the
  coordinator in this stack.
- Changing the user-visible run ID or the public `spawn_*` MCP contracts.
- Automatically replaying an attempt whose side effects are uncertain.
- Supporting nested subagent spawning; existing policy remains unchanged.
- Removing all legacy run files during this stack.

## 5. Design

### 5.1 Components and ownership

The code is split along lifecycle boundaries rather than by transport:

| Component | Responsibility |
|---|---|
| `run_coordinator/__init__.py` | Typed public port: records, commands, transitions, lease and outbox operations |
| `run_coordinator/memory.py` | Deterministic in-memory implementation used by contract and lifecycle tests |
| `run_coordinator/sqlite.py` | SQLite schema, migration runner, transactions, WAL setup, and fenced updates |
| `run_coordinator/legacy.py` | Shadow writer, parity checker, importer, and compatibility artifact policy |
| `subagent_lifecycle.py` | Terminal arbitration, shielded report ownership, and teardown gates |
| `subagent_scheduler.py` | Admission, queue order, capacity lease, and drain readiness |
| `subagent.py` | Backward-compatible facade plus local ACP/session executor |
| gateway delivery adapter | Claims outbox events, calls the existing parent/DM delivery paths, and acknowledges accepted events |

`SubagentManager` continues to expose the current methods while callers migrate.
It delegates lifecycle decisions rather than allowing transports or persistence
adapters to mutate private fields directly.

No coordinator implementation may import the gateway or `SessionManager`.
The executor may depend on the coordinator port, but the port does not depend on
the executor. This keeps persistence replaceable and prevents the database from
becoming a second God module.

### 5.2 Typed vocabulary

The coordinator API uses string-backed enums at persistence and transport
boundaries and frozen dataclasses internally.

**Desired state** records operator intent:

- `run`
- `cancel`
- `release`

**Observed state** records executor progress:

- `accepted`
- `queued`
- `starting`
- `running`
- `terminal`

**Terminal outcome** is nullable until `observed_state=terminal`:

- `completed`
- `failed`
- `stopped`
- `interrupted`

**Command operation**:

- `spawn`
- `continue`
- `steer`
- `cancel`
- `release`

**Delivery state**:

- `pending`
- `claimed`
- `delivered`

The public transition methods return typed results with a machine-readable
reason. They do not return unvalidated dictionaries. An unknown stored enum or
newer schema version fails closed for mutation and remains inspectable for
diagnosis.

### 5.3 Canonical schema

SQLite lives under the Kiro Crew data home beside the existing subagent
registry. The initial schema has four tables.

#### `runs`

| Column | Meaning |
|---|---|
| `run_id TEXT PRIMARY KEY` | Existing externally visible subagent ID |
| `parent_session TEXT NOT NULL` | Parent delivery address |
| `agent TEXT NOT NULL` | Agent/template selection |
| `task TEXT NOT NULL` | Accepted task text |
| `conversation_key TEXT NOT NULL` | Continuation lineage, empty for a fresh conversation |
| `desired_state TEXT NOT NULL` | Latest accepted intent |
| `observed_state TEXT NOT NULL` | Executor progress |
| `outcome TEXT` | Terminal outcome or null |
| `result_path TEXT NOT NULL` | Path to the canonical full result artifact |
| `error TEXT NOT NULL` | Sanitized terminal detail |
| `attempt INTEGER NOT NULL` | Execution attempt, starting at one |
| `version INTEGER NOT NULL` | Optimistic transition version |
| `owner_id TEXT` | Gateway incarnation currently allowed to execute |
| `lease_expires_at REAL` | Wall-clock expiry used for restart takeover |
| `lease_epoch INTEGER NOT NULL` | Monotonic fencing token |
| `created_at REAL NOT NULL` | Creation time |
| `updated_at REAL NOT NULL` | Last durable transition time |
| `terminal_at REAL` | Terminal transition time |

Task text is already stored in `state.json`; moving it into the owner-only
database does not broaden the persisted data class. Payloads are never written
to logs or metrics.

#### `commands`

| Column | Meaning |
|---|---|
| `command_id TEXT PRIMARY KEY` | Stable caller-generated ID |
| `idempotency_key TEXT UNIQUE NOT NULL` | Retry identity |
| `run_id TEXT NOT NULL` | Target run |
| `operation TEXT NOT NULL` | Typed command operation |
| `payload_json TEXT NOT NULL` | Versioned validated payload |
| `payload_hash TEXT NOT NULL` | Detects key reuse with different input |
| `status TEXT NOT NULL` | `pending`, `claimed`, `applied`, or `rejected` |
| `attempt INTEGER NOT NULL` | Dispatch attempt count |
| `owner_id TEXT` | Current command claimant |
| `claim_expires_at REAL` | Independent command-claim expiry |
| `claim_epoch INTEGER` | Fences stale command effects without changing the run lease |
| `result_json TEXT NOT NULL` | Bounded transport response used for exact replay/lookup |
| `created_at REAL NOT NULL` | Acceptance time |
| `updated_at REAL NOT NULL` | Last transition time |

Reusing an idempotency key with the same payload returns the original command
decision and run ID. Reusing it with a different payload is a typed conflict and
never mutates the run.

Control commands may target runs created before coordinator cutover, so the
command target is not constrained by a foreign key to `runs`. Execution
submissions still create their run and command atomically.

#### `outbox`

| Column | Meaning |
|---|---|
| `event_id TEXT PRIMARY KEY` | Stable delivery identity |
| `run_id TEXT NOT NULL` | Terminal run |
| `destination TEXT NOT NULL` | Parent session or fallback route |
| `event_type TEXT NOT NULL` | Versioned completion event type |
| `payload_json TEXT NOT NULL` | Bounded completion envelope; full text remains in `result.txt` |
| `status TEXT NOT NULL` | Delivery state |
| `attempts INTEGER NOT NULL` | Delivery attempts |
| `available_at REAL NOT NULL` | Earliest retry time |
| `claim_owner TEXT` | Delivery claimant |
| `claim_expires_at REAL` | Claim expiry |
| `claim_epoch INTEGER NOT NULL` | Monotonic delivery fencing token |
| `created_at REAL NOT NULL` | Commit time |
| `delivered_at REAL` | Accepted-delivery time |

`event_id` is included in the injected completion envelope. Consumers may use
it for deduplication. During compatibility mode, destinations that cannot yet
deduplicate still receive at-least-once delivery, matching restart recovery's
existing possibility of redelivery.

#### `metadata`

`metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)` stores the schema version
and migration bookkeeping. Migrations are ordered, transactional, forward-only,
and idempotent. Startup refuses coordinator-authoritative mutation if the file's
schema is newer than the binary understands.

### 5.4 Coordinator API

The first public port is intentionally small:

```python
class RunCoordinator(Protocol):
    async def submit(self, request: SubmitRun) -> SubmitResult: ...
    async def submit_control(self, request: SubmitControl) -> CoordinatorResult[CommandReceipt]: ...
    async def get_command_by_key(self, key: str) -> CommandReceipt | None: ...
    async def claim_command(self, command_id: str, owner: OwnerLease) -> CommandClaim | None: ...
    async def finish_command(
        self, fence: CommandFence, status: CommandStatus,
        rejection_reason: str = "", result_json: str = ""
    ) -> CoordinatorResult[RunCommand]: ...
    async def claim_commands(self, owner: OwnerLease, limit: int) -> list[CommandClaim]: ...
    async def mark_starting(
        self, command: RunCommand, fence: RunFence, expected_version: int
    ) -> CoordinatorResult[RunRecord]: ...
    async def mark_running(
        self, run_id: str, fence: RunFence, expected_version: int
    ) -> CoordinatorResult[RunRecord]: ...
    async def complete(
        self, completion: RunCompletion, fence: RunFence, expected_version: int
    ) -> CoordinatorResult[OutboxEvent]: ...
    async def renew(self, run_id: str, fence: RunFence, until: float) -> bool: ...
    async def claim_outbox(
        self,
        owner: OwnerLease,
        limit: int,
        event_id: str = "",
        acknowledgement: bool = False,
    ) -> list[OutboxEvent]: ...
    async def release_outbox(
        self, fence: DeliveryFence, available_at: float
    ) -> CoordinatorResult[OutboxEvent]: ...
    async def mark_delivered(self, fence: DeliveryFence) -> CoordinatorResult[OutboxEvent]: ...
    async def get_run(self, run_id: str) -> RunRecord | None: ...
```

Implementation details may add query methods, but transports only depend on
typed commands and records. Synchronous SQLite work is wrapped in bounded
`asyncio.to_thread()` calls; a connection is not shared concurrently across
event-loop tasks.

`CommandClaim` contains the claimed command and an independent `CommandFence`;
execution-dispatch claims may also carry an acquired `RunFence`.
`DeliveryFence` contains `event_id`, `owner_id`, and `claim_epoch`; reclaiming an
expired event increments the epoch, so an older delivery task owned by the same
gateway incarnation cannot acknowledge the newer claim. Lifecycle mutations
carry `expected_version`; lease renewal does not advance that lifecycle version.
Rejected, stale, conflicting, and unchanged mutations return a typed
`CoordinatorResult` with a machine-readable decision and reason rather than an
unvalidated dictionary or transport-specific exception.

### 5.5 Transactions and state transitions

#### Admission

1. The caller chooses `run_id`, `command_id`, and `idempotency_key` before the
   HTTP boundary.
2. One transaction inserts the accepted run and pending command.
3. If the key already exists with the same payload hash, the coordinator returns
   the existing result.
4. If admission rejects the request, the transaction records a terminal failed
   run plus a rejected command and reason. The run is queryable by the ID the
   caller already received, but it never becomes executable work.

Batch submission may initially call `submit()` per member. Once each call is
idempotent, a later additive `submit_many()` can make roster acceptance one
transaction without changing the MCP contract.

#### Dispatch and execution

1. The scheduler claims pending commands and a capacity slot.
2. The coordinator sets `owner_id`, advances `lease_epoch`, and returns the
   fence `(run_id, owner_id, lease_epoch)`.
3. Before starting a child, the executor transitions the run to `starting`
   using that fence; after process/session creation it marks `running`.
4. Every later executor mutation includes the fence. A stale owner or epoch
   receives a typed `stale_fence` result and cannot overwrite the new owner.

#### Terminal completion

One transaction:

1. verifies the fence and legal transition;
2. writes `observed_state=terminal` and the outcome;
3. inserts a stable outbox event (pending for asynchronous delivery, or already
   delivered when the synchronous response is the delivery); and
4. marks the execution command applied.

Repeating the same completion returns the existing event. A conflicting second
outcome is rejected and recorded as a diagnostic; first durable terminal
outcome wins, matching the current terminal-record guard.

The execution command's same-fence empty-result fill also verifies the current
run owner and lease epoch. Recovery takeover therefore fences an old executor
even when its command claim record still carries the earlier matching epoch.
Synchronous non-batch admission rejection commits an already-delivered event so
the counted HTTP error is not followed by a duplicate parent turn. Batch
rejections keep a pending event with their wave metadata and route through the
normal completion consumer.

#### Delivery

The gateway adapter claims an available event for a bounded interval, increments
its delivery claim epoch, calls the existing parent injection or fallback path,
and marks it delivered only after that path accepts it and the delivery fence
still matches. A crash after acceptance but before acknowledgement may redeliver
the same `event_id`; it never re-executes the run.

Failed delivery releases only the matching fenced claim and persists its next
`available_at`; a stale delivery task cannot postpone a newer claim. Retry
backoff is bounded and persisted. Permanent routing failure keeps the
event inspectable and follows the existing owner-DM fallback policy; it does not
erase the completion.

### 5.6 Ownership, leases, and fencing

Every gateway start generates a random `owner_id`. The supported deployment
still runs one gateway, but ownership is represented explicitly:

- Claiming an unowned or expired run increments `lease_epoch` in the same
  transaction that sets the owner and lease expiry.
- Heartbeats renew only when both owner and epoch match.
- All executor-originated state transitions require that same fence.
- A later owner can take over an expired lease; the former owner cannot commit
  after takeover even if it wakes up.
- Lease expiry grants authority to reconcile, not authority to replay. Recovery
  first checks process identity, result artifacts, and durable state.

Wall clock is acceptable for persisted expiry, while elapsed waits inside one
process use monotonic time. Tests inject a clock; production code does not sleep
to prove lease behavior.

### 5.7 Uncertain execution safety

The safety policy is intentionally asymmetric:

- **Delivery is at least once.** It is safe to retry a stable completion event.
- **Execution is not blindly retried.** If a crash leaves evidence that a child
  may have run but no terminal outcome, recovery records `interrupted`, retains
  partial output, and tells the parent. It does not start attempt two
  automatically.
- Existing zero-tool unexpected-cancel recovery remains the only automatic
  continuation exception. It stays behind its side-effect gate until a future
  RFC can express tool effects durably.

This policy prevents the ledger from turning a transport retry into duplicated
external side effects.

### 5.8 Additive legacy compatibility

Authority changes by artifact and operation, never all at once:

| Phase | Run/command authority | Delivery authority | Legacy artifacts |
|---|---|---|---|
| Before stack | In-memory manager + files | In-memory callbacks + tombstone ordering | Canonical |
| Shadow mode | Existing behavior | Existing behavior | Canonical; coordinator compared only |
| Command cutover | Coordinator | Existing callbacks | `state.json`/tombstones mirrored |
| Outbox cutover | Coordinator | Coordinator outbox | Mirrors retained for downgrade and `spawn_status` |
| Recovery cutover | Coordinator first, importer fallback | Coordinator outbox first | Old folders imported once; `result.txt` remains canonical |

Parity mismatches are observable and fail back to the existing authority while
shadow mode is active. No automatic database-to-file repair happens during
shadow comparison because that could hide the defect being measured.

The legacy importer is idempotent by `run_id`. It imports only validated paths
under the subagent registry, never follows an unvalidated ID, and records the
source artifact version. A corrupt file remains untouched and produces a
diagnostic instead of an invented state.

### 5.9 Configuration and rollback

The stack uses one internal rollout mode with three values:

- `legacy`: no coordinator reads or writes;
- `shadow`: legacy-authoritative plus coordinator writes and parity checks;
- `coordinator`: coordinator-authoritative plus legacy compatibility mirrors.

The default advances only in the PR that has the tests and migration required
for that authority boundary. This is an internal migration control, not a
long-lived user feature flag.

Rollback from `coordinator` to `legacy` is supported while mirrors are emitted.
Rollback never deletes the SQLite database. If a schema is newer than the old
binary, the old binary ignores it and continues from the compatible files.
The final removal of metadata mirrors requires a separate future RFC or release
decision with an explicit downgrade window; it is not part of this stack.

## 6. Migration plan: seven stacked PRs

Each PR is one local branch and one logical commit. Every branch is based on the
previous branch, and each PR is independently reviewable. Implementation PRs
update this RFC's audit metadata and the current-behavior subagent spec in the
same commit.

### PR 1 — design and stack contract

Branch: `run-coordinator-plan`

- Add this RFC and record the current architecture measurements.
- Define the authority table, failure policy, schema, compatibility window,
  stack order, and exit criteria.
- Correct RFC index drift discovered during the audit.

**Exit criteria:** documentation lint passes; the RFC contains no unresolved
decision required by PR 2; no runtime behavior changes.

### PR 2 — typed lifecycle port and characterization tests

Branch: `run-coordinator-types`

- Add typed run, command, outcome, fence, and outbox records.
- Add the `RunCoordinator` protocol and an in-memory implementation.
- Characterize current admission, terminal arbitration, slot release, and
  completion-delivery behavior through the new public boundary.
- Wire the default facade to the in-memory coordinator without changing the
  legacy authority or externally visible behavior.

**Exit criteria:** one coordinator contract suite passes against memory;
existing subagent tests pass unchanged except for imports/fixtures; duplicate
terminal transitions, optimistic-version conflicts, stale execution fences, and
stale delivery fences have deterministic typed results.

### PR 3 — extract scheduling and finalization boundaries

Branch: `run-coordinator-boundaries`

- Extract admission/queue capacity into `subagent_scheduler.py`.
- Extract the four-guard terminal protocol into `subagent_lifecycle.py`.
- Keep `SubagentManager` as the compatibility facade and ACP/session executor.
- Replace private-field tests for moved behavior with boundary tests, retaining
  targeted regression tests for the known reap/report interleavings.

**Exit criteria:** `SubagentManager` no longer owns queue policy, report-task
lifetime, or teardown-gate state; current public and wire behavior is
byte-for-byte compatible; race tests prove one terminal record, one report
claim, and one slot release.

### PR 4 — SQLite store, migrations, and shadow parity

Branch: `run-coordinator-shadow`

- Implement schema v1, migrations, WAL, busy timeout, owner-only permissions,
  integrity checks, and the SQLite coordinator contract.
- Add the coordinator directory to the sensitive-path floor and document that
  trust boundary in the security spec.
- Add shadow writes after successful legacy transitions.
- Compare coordinator and legacy views at stable boundaries and emit bounded
  mismatch diagnostics.
- Keep SQLite work off the event loop.

**Exit criteria:** the same contract suite passes against memory and SQLite;
schema creation and upgrade are crash-safe and idempotent; shadow mismatch
tests do not mutate legacy authority; an event-loop responsiveness test detects
accidental synchronous database I/O.

### PR 5 — coordinator-authoritative commands and admission

Branch: `run-coordinator-commands`

- Generate stable command and idempotency IDs before the MCP-to-HTTP boundary.
- Make spawn and continuation admission coordinator-authoritative.
- Route steer, cancel, and release through idempotent commands.
- Replace lost-submission inference with lookup of the durable command decision;
  retain compatibility handling for old callers without keys.

**Exit criteria:** repeating the same submission returns the same run ID and
does not start a second child; key/payload conflicts fail closed; queue capacity
and approval outcomes match legacy behavior; old callers still work.

**Local status:** implemented. Execution and control commands use independent
durable claim fences; controls do not mutate a live executor lease. SQLite
schema v3 stores claim/result state and supports commands targeting pre-cutover
runs. The MCP/gateway boundary validates stable identity and resolves uncertain
responses through durable lookup. A claimed control without a stored result is
never replayed after expiry: the authority returns an outcome-uncertain response
because the legacy side effect may already have happened. Queued and
approval-waiting executions remain `CLAIMED` until their manager task actually
starts; that boundary durably applies the command before releasing its
pre-execution lease. Exact retries of rejected executions replay the stored
response instead of falling through to a generic legacy conflict.

### PR 6 — transactional completion and delivery outbox

Branch: `run-coordinator-outbox`

- Commit terminal run state and its delivery event in one transaction.
- Deliver through the gateway adapter and acknowledge only after parent/fallback
  acceptance.
- Include stable `event_id` in completion envelopes.
- Continue writing compatible tombstones and `result.txt` retention metadata.

**Exit criteria:** crash tests at every completion/delivery boundary show no
lost terminal event; redelivery never repeats execution; existing completion
envelope consumers ignore or use the additive `event_id`; delivery retry and
fallback remain bounded.

**Local status:** implemented. Exact execution claims acquire a renewable run
lease while controls retain independent command-only fences. The manager commits
`starting` before child startup, `running` before prompting, and terminal state
plus one outbox row before callbacks. Direct acceptance is fenced and
acknowledged; dashboard-queued and digest-held events stay pending until their
existing consumption hooks settle the same event. Payloads contain bounded
summary/routing data and the full-result path.

### PR 7 — coordinator-first restart recovery and legacy import

Branch: `run-coordinator-recovery`

- Acquire expired leases with fencing and reconcile coordinator runs first.
- Import legacy-only folders idempotently, then apply the same recovery policy.
- Recover pending outbox events independently from uncertain executions.
- Add a small real-subprocess crash/restart suite in addition to deterministic
  fake-runtime tests.

**Exit criteria:** abrupt gateway termination after accept, child start,
terminal commit, destination acceptance, and delivery acknowledgement converges
to a documented state; a stale owner cannot commit after takeover; uncertain
execution is reported once without automatic replay; legacy-only installs
upgrade without losing retained results.

**Local status:** implemented. Schema v4 records the source version on imported
runs, and schema v5 records process identity and ownership inside the protected
coordinator store. The importer reads known legacy fields without modifying
source files and is idempotent against native coordinator rows; agent-writable
legacy process fields never authorize termination, non-finite timestamps are
skipped per folder, and legacy destinations never manufacture pending outbox
work. A dedicated child does not receive a prompt until its fenced process
identity is durably stored; failure aborts execution while the legacy state
mirror remains best-effort. Recovery claims expired
nonterminal leases with a fresh epoch, signals only a coordinator-owned PID with
an exact fenced start identity, retains partial output, commits `interrupted`,
emits a SEL termination audit, and drains terminal outbox work separately. The
periodic reaper retries leases that had not expired at startup while excluding
locally active run IDs. A hermetic sleeper-process test exercises the real
takeover/termination path.

### Deferred cleanup after the compatibility window

Stopping `state.json` and `tombstone.json` mirrors is deliberately outside the
seven-PR stack. `result.txt` remains. Cleanup requires field evidence that no
supported downgrade or operator workflow depends on the metadata files and must
be proposed separately.

## 7. Testing strategy

### 7.1 Coordinator contract suite

Run the same behavioral suite against the in-memory and SQLite implementations:

- legal and illegal state transitions;
- same-key/same-payload idempotency and key/payload conflict;
- optimistic version conflicts;
- owner lease renewal, expiry, takeover, and stale-fence rejection;
- terminal completion/outbox atomicity;
- outbox claim expiry and stable redelivery identity;
- schema creation, migration, newer-schema refusal, and corruption handling.

### 7.2 Deterministic lifecycle tests

Use a deterministic fake runtime rather than broad `MagicMock` fixtures for the
new public boundaries. Pin invocation traces for queue refill and terminal race
interleavings. Inject clocks and failure points; do not prove correctness with
sleep durations.

### 7.3 Crash-window matrix

For each transaction boundary, terminate or fault the owning adapter and restart:

1. before and after command acceptance;
2. before and after child-start marking;
3. during output append;
4. before and after terminal/outbox commit;
5. before and after destination acceptance;
6. before and after delivered acknowledgement;
7. before and after lease takeover.

The assertion is convergence, not merely process survival: execution happens no
more than allowed by the uncertain-execution policy, and every committed terminal
outcome retains a pending or delivered event.

### 7.4 Compatibility and integration

- Golden tests compare public HTTP/MCP responses and websocket lifecycle events.
- Import tests cover running, completed, tombstoned, corrupt, retained, and
  continuable legacy folders.
- One small subprocess test starts a real child, terminates the gateway harness,
  and verifies restart reconciliation and process cleanup.
- Cross-platform tests use `platform_compat` for liveness, signals, permissions,
  and process trees.

## 8. Observability

The coordinator exposes bounded, low-cardinality metrics:

- accepted/rejected/idempotent-conflict commands by operation and reason;
- command queue delay and run state counts;
- lease acquisition, expiry, takeover, and stale-fence rejection;
- pending outbox age, attempts, and delivery outcome;
- shadow parity mismatch by field class;
- coordinator transaction latency and busy/error counts;
- recovery and importer outcomes.

Metrics never contain task text, result text, session IDs, command payloads, or
run IDs. Logs may name a shortened run ID where existing diagnostics already do,
but payloads stay redacted.

## 9. Backward compatibility

- Existing run IDs, MCP tools, internal HTTP paths, and normal response shapes
  remain valid. New identifiers and `event_id` are additive.
- `spawn_status` continues to read retained `result.txt` and current metadata
  throughout the stack.
- Existing subagent folders are imported lazily and never deleted by import.
- Conversation retention and release remain compatible with current
  `state.json` behavior while mirrors are active.
- Downgrading during the compatibility window uses the legacy mirrors; the
  database remains untouched for a later upgrade.
- SQLite failure in `shadow` mode degrades to legacy authority with a diagnostic.
  SQLite failure in `coordinator` mode fails closed for new mutation rather than
  accepting work that cannot be recorded durably.

## 10. Security considerations

- The database is created in a dedicated directory below the Kiro Crew data
  home. PR 4 adds that directory to `_SENSITIVE_HOME_DIRS` under every known
  data-home prefix, so agent file tools and shell commands cannot read, replace,
  or delete the database, its WAL/SHM files, or migration sidecars. The security
  spec and its path tests change in that same commit.
- Its directory and file use the cross-platform owner-only helpers in
  `platform_compat`; permission tightening is not a POSIX-only no-op.
- Run IDs and imported paths are validated with the same containment rule as
  `_agent_dir()`; SQL parameters are bound, never interpolated.
- Task and bounded delivery payloads are persisted because equivalent content
  is already persisted in run files and session history. They are excluded from
  metrics, ordinary logs, integrity errors, and migration diagnostics.
- The database provides durability, not a trust boundary. Governance and tool
  approval remain at their current enforcement points and are not inferred from
  coordinator state.
- Owner leases prevent stale local processes from mutating state; they are not
  authentication for remote workers. A distributed backend requires a separate
  threat model and RFC.

## 11. Alternatives considered

### 11.1 Keep adding recovery branches to `SubagentManager`

This preserves short-term locality but leaves command acceptance, execution,
delivery, and recovery represented by different flags and files. It does not
make submission idempotent or completion/outbox atomic, and makes each new
failure path harder to audit.

### 11.2 Extract classes without adding a durable ledger

Splitting the God module alone improves maintainability and is included early in
the stack, but it cannot resolve the uncertain-acceptance seam or distinguish
pending delivery from pending execution after restart.

### 11.3 Replace the folder registry with SQLite in one PR

A flag-day migration would combine schema risk, behavioral refactoring, recovery
changes, and compatibility removal. It would also discard `result.txt`, which is
useful for operators and on-demand transcript reads. The additive authority
ladder keeps every cutover observable and reversible.

### 11.4 Start with a network queue or distributed database

Kiro Crew is presently a personal, single-gateway system. A remote control plane
would add deployment, authentication, partition, and consistency requirements
before the local lifecycle has one explicit contract. The coordinator port and
fence fields preserve a future seam without paying that cost now.

### 11.5 Use SQLite as a work queue but keep callback delivery

This fixes command acceptance but leaves the most important crash window between
terminal state and parent delivery. The transactional outbox is small once the
run ledger exists and gives completion a complete durable contract.

## 12. Open questions

There are no unresolved decisions blocking PR 2. The following are deliberately
deferred beyond this stack:

- whether TaskRunner, workflows, or Crew Mode should adopt the coordinator port;
- when compatibility evidence is sufficient to stop metadata mirrors;
- whether a future distributed implementation should use SQLite replication, a
  service database, or a queue-backed coordinator.

Each requires its own measurements and, if pursued, an RFC update or a separate
RFC. None changes the local additive design above.
