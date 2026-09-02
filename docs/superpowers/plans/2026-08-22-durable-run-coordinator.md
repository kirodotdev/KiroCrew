# Durable Run Coordinator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a typed, SQLite-backed run lifecycle ledger and split scheduling
and finalization out of `SubagentManager` without breaking current subagent APIs
or legacy recovery artifacts.

**Architecture:** `SubagentManager` remains the compatibility facade and local
ACP executor. A typed `RunCoordinator` port owns run/command/outbox state; an
in-memory adapter establishes behavior before a SQLite adapter shadow-writes and
then becomes authoritative. Scheduling and terminal claims move into focused
objects, while `state.json`, `tombstone.json`, and `result.txt` remain compatible
through the migration.

**Tech Stack:** Python 3.10+, asyncio, frozen dataclasses, string enums, stdlib
`sqlite3`, pytest/pytest-asyncio, existing
`platform_compat` permission and process helpers.

**Spec:** `docs/request-for-change/rfc-durable-run-coordinator.md`

## Global Constraints

- Preserve the public MCP, HTTP, websocket, parent-injection, approval,
  governance, session, and result-retention contracts.
- Write every production behavior test-first and observe the intended failure
  before implementation.
- Keep synchronous SQLite and legacy filesystem work off the asyncio event loop.
- Use the existing visible run ID; never mint a second external identity.
- Retry delivery with a stable event ID; never blindly retry uncertain execution.
- Route process, signal, permission, and locking behavior through
  `platform_compat` on macOS, Linux, and Windows.
- Keep `result.txt` as the canonical full-output artifact.
- Do not touch `CHANGELOG.md` and do not push any branch.

---

## Stack and file map

| PR | Branch | Primary files |
|---|---|---|
| 1 | `run-coordinator-plan` | RFC, this plan, RFC index audit |
| 2 | `run-coordinator-types` | `run_coordinator/models.py`, `memory.py`, `__init__.py`, contract tests, facade injection |
| 3 | `run-coordinator-boundaries` | `subagent_lifecycle.py`, `subagent_scheduler.py`, `subagent.py`, reap/scheduler tests |
| 4 | `run-coordinator-shadow` | `run_coordinator/sqlite.py`, `legacy.py`, security floor/spec/tests, SQLite contract tests |
| 5 | `run-coordinator-commands` | MCP/HTTP command IDs, manager admission, messaging tests, subagent spec |
| 6 | `run-coordinator-outbox` | terminal transaction, gateway delivery adapter, injected envelope, delivery tests/specs |
| 7 | `run-coordinator-recovery` | coordinator-first recovery, importer, lease takeover, crash-window tests/spec |

### Stable interfaces used across the stack

```python
class RunCoordinator(Protocol):
    async def submit(self, request: SubmitRun) -> SubmitResult: ...
    async def claim_commands(
        self, owner: OwnerLease, limit: int
    ) -> list[CommandClaim]: ...
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
        self, owner: OwnerLease, limit: int
    ) -> list[OutboxEvent]: ...
    async def release_outbox(
        self, fence: DeliveryFence, available_at: float
    ) -> CoordinatorResult[OutboxEvent]: ...
    async def mark_delivered(
        self, fence: DeliveryFence
    ) -> CoordinatorResult[OutboxEvent]: ...
    async def get_run(self, run_id: str) -> RunRecord | None: ...
```

`SubmitRun` carries `run_id`, `command_id`, `idempotency_key`, `payload_hash`,
parent, agent, task, and conversation key. `RunFence` carries `run_id`,
`owner_id`, and `lease_epoch`. `CommandClaim` pairs the command with that fence.
`DeliveryFence` carries `event_id`, `owner_id`, and `claim_epoch`. Every mutation
returns a typed `CoordinatorResult[T]` whose decision is `applied`, `unchanged`,
or `rejected` and whose reason distinguishes idempotency conflict, stale fence,
version conflict, and invalid transition without throwing transport-specific
exceptions.

---

### Task 1: PR 1 — record the implementation plan

**Files:**

- Create: `docs/superpowers/plans/2026-08-22-durable-run-coordinator.md`
- Modify: `docs/request-for-change/rfc-durable-run-coordinator.md`

**Interfaces:**

- Consumes: the approved RFC at revision v1.
- Produces: exact branch, file, test, and verification boundaries for PRs 2–7.

- [x] **Step 1: Add this plan and link it from the RFC.**

  Add a `Detailed implementation plan` link next to the RFC's related docs.

- [x] **Step 2: Verify documentation.**

  Run `bash scripts/docs-lint.sh` and `git diff --check`.
  Expected: both exit zero.

- [x] **Step 3: Amend PR 1's local commit.**

  Run `git commit --amend --no-edit` so PR 1 remains one logical commit.

---

### Task 2: PR 2 — typed coordinator port and in-memory contract

**Files:**

- Create: `src/kiro_crew/run_coordinator/models.py`
- Create: `src/kiro_crew/run_coordinator/memory.py`
- Create: `src/kiro_crew/run_coordinator/__init__.py`
- Create: `test/test_run_coordinator_contract.py`
- Modify: `src/kiro_crew/subagent.py:1347-1424`
- Modify: `docs/request-for-change/rfc-durable-run-coordinator.md`

**Interfaces:**

- Consumes: the stable protocol and exception names above.
- Produces: `RunCoordinator`, all typed records/enums, `MemoryRunCoordinator`,
  and optional `coordinator=` injection on `SubagentManager`.

- [x] **Step 1: Write failing model and submission tests.**

  Parameterize a coordinator factory initially with `MemoryRunCoordinator` and
  assert: first submit returns `created=True`; same key/hash returns the same run
  with `created=False`; same key/different hash returns a typed idempotency
  conflict without mutation; a
  rejected request creates a queryable terminal failed run and rejected command.

- [x] **Step 2: Run the contract test and verify RED.**

  Run `python -m pytest test/test_run_coordinator_contract.py -n0 -q`.
  Expected: collection fails because `kiro_crew.run_coordinator` does not exist.

- [x] **Step 3: Implement typed models and in-memory submission.**

  Use `str, Enum` values from the RFC and frozen dataclasses. Store records in
  private dictionaries protected by one `asyncio.Lock`; copy records with
  `dataclasses.replace` instead of mutating returned objects.

- [x] **Step 4: Write failing claim, fence, completion, and outbox tests.**

  Assert command claim assigns owner and epoch; matching fences advance
  `starting -> running -> terminal`; stale fences raise; completion returns one
  stable outbox event; reapplying identical completion returns that event;
  conflicting completion is rejected; an expired outbox claim increments
  `claim_epoch`, its prior delivery fence is rejected, and delivered events no
  longer claim.

- [x] **Step 5: Run the new tests and verify RED.**

  Expected: failures name unimplemented transition methods, not fixture errors.

- [x] **Step 6: Implement the minimal in-memory state machine.**

  Centralize legal transitions in one map and validate terminal/outcome
  coherence before replacing a record. Use a caller-injected clock and ID
  factory so tests assert exact values without sleeping.

- [x] **Step 7: Add facade injection without changing authority.**

  Add `coordinator: RunCoordinator | None = None` to `SubagentManager.__init__`
  and store `self._coordinator = coordinator or MemoryRunCoordinator()`. Do not
  route production transitions through it yet.

- [x] **Step 8: Verify and commit PR 2.**

  Run focused tests, format only new/touched files, run flake8/mypy on those
  files, update RFC audit metadata, and commit:
  `feat: add typed run coordinator contract`.

---

### Task 3: PR 3 — extract finalization and scheduling boundaries

**Files:**

- Create: `src/kiro_crew/subagent_lifecycle.py`
- Create: `src/kiro_crew/subagent_scheduler.py`
- Create: `test/test_subagent_lifecycle.py`
- Create: `test/test_subagent_scheduler.py`
- Modify: `src/kiro_crew/subagent.py`
- Modify: `test/test_subagent_reap_race.py`
- Modify: `test/test_subagent_scale.py`
- Modify: `docs/system-specs/modules/subagent.md`
- Modify: `docs/request-for-change/rfc-durable-run-coordinator.md`

**Interfaces:**

- Consumes: objects with `_recovering`, `_finalized`, and `_slot_released`
  fields; queued spawn dictionaries remain opaque payloads.
- Produces: `RunFinalizer.claim_report()`, `claim_slot()`, `rearm_slot()` and
  `SubagentScheduler` capacity/queue operations.

- [x] **Step 1: Write failing finalizer tests.**

  Move the pure claim cases out of the manager fixture: exactly one report
  claim, recovery withholding without consumption, superseding recovery,
  exactly one slot claim, and rearming only for an admitted recovery attempt.

- [x] **Step 2: Verify RED, then implement `RunFinalizer`.**

  Run `python -m pytest test/test_subagent_lifecycle.py -n0 -q`; verify missing
  module failure; implement the three synchronous no-await methods; rerun green.

- [x] **Step 3: Write failing scheduler tests.**

  Assert FIFO queue order, maximum-capacity admission, one-time release, parent
  depth, batch presence, removal by preassigned ID, and queue payload identity.

- [x] **Step 4: Verify RED, then implement `SubagentScheduler`.**

  Keep stagger timing and actual `spawn()` calls in the facade; the scheduler
  owns only queue and capacity policy. Expose read-only `running_count` and
  `queued` views plus explicit `admit`, `release`, `enqueue`, `pop_next`,
  `remove`, `count_parent`, and `has_batch` operations.

- [x] **Step 5: Delegate manager behavior through the boundaries.**

  Keep `_claim_finalize`, `_release_slot`, `_running_count`, and `_queue` as
  compatibility shims for existing callers/tests, but make each forward to the
  extracted object. Replace production mutations with scheduler operations.

- [x] **Step 6: Run race and scale regressions.**

  Run the new tests plus `test/test_subagent_reap_race.py` and the queue/lost-
  submission classes in `test/test_subagent_scale.py`, all with `-n0`.

- [x] **Step 7: Update the subagent spec and commit PR 3.**

  Document the new ownership boundary without changing behavior. Commit:
  `refactor: extract subagent lifecycle boundaries`.

---

### Task 4: PR 4 — SQLite implementation and shadow parity

**Files:**

- Create: `src/kiro_crew/run_coordinator/sqlite.py`
- Create: `src/kiro_crew/run_coordinator/legacy.py`
- Create: `test/test_run_coordinator_sqlite.py`
- Modify: `test/test_run_coordinator_contract.py`
- Modify: `src/kiro_crew/security.py`
- Modify: `test/test_security.py`
- Modify: `docs/system-specs/modules/security.md`
- Modify: `docs/system-specs/modules/subagent.md`
- Modify: `docs/request-for-change/rfc-durable-run-coordinator.md`

**Interfaces:**

- Consumes: the complete `RunCoordinator` contract from PR 2.
- Produces: `SQLiteRunCoordinator`, `ShadowRunCoordinator`, schema version 1,
  and owner-only/sensitive coordinator storage.

- [x] **Step 1: Add SQLite to the contract factory and verify RED.**

  Construct `SQLiteRunCoordinator(tmp_path / "coordinator" / "runs.sqlite3",
  clock=fake_clock, id_factory=fake_ids)`. Expected: import failure.

- [x] **Step 2: Write failing schema and security tests.**

  Assert four tables and schema version, WAL and busy timeout, transactional
  re-open, newer-schema refusal, corrupt-file refusal, owner-only directory/file
  helpers, and sensitive matching for the coordinator directory plus sidecars
  under current and legacy data-home prefixes.

- [x] **Step 3: Implement schema and synchronous transaction core.**

  Use one stdlib `sqlite3` connection per worker call, bound SQL parameters,
  explicit `BEGIN IMMEDIATE`, foreign keys, Python enum validation, and
  transactional migration metadata. Never keep a connection on the event-loop
  object and never use `executescript()` inside a migration transaction.

- [x] **Step 4: Add async wrappers and verify off-loop behavior.**

  Every protocol method calls its synchronous counterpart through
  `asyncio.to_thread`. A test monkeypatches the sync worker to record thread ID
  and asserts it differs from the event-loop thread; it does not use durations.

- [x] **Step 5: Implement and test shadow parity.**

  `ShadowRunCoordinator` calls the legacy-authoritative coordinator first,
  mirrors successful transitions to SQLite, compares normalized records at
  stable boundaries, and reports a bounded `ParityMismatch` callback without
  repairing or failing legacy behavior.

- [x] **Step 6: Update security/subagent specs and commit PR 4.**

  Run coordinator/security tests, docs lint, formatting, flake8, and mypy.
  Commit: `feat: add sqlite run coordinator shadow store`.

---

### Task 5: PR 5 — coordinator-authoritative command admission

**Files:**

- Modify: `src/kiro_crew/mcp_tools/spawn.py`
- Modify: `src/kiro_crew/dashboard/handlers/messaging.py`
- Modify: `src/kiro_crew/subagent.py`
- Create: `test/test_run_coordinator_admission.py`
- Modify: `test/test_subagent_scale.py`
- Modify: `docs/architecture/mcp.md`
- Modify: `docs/system-specs/modules/subagent.md`
- Modify: `docs/request-for-change/rfc-durable-run-coordinator.md`

**Interfaces:**

- Consumes: `RunCoordinator.submit()` and the existing preassigned run ID.
- Produces: additive `command_id`, `idempotency_key`, and `payload_hash` fields on
  authenticated internal spawn requests; durable command lookup on uncertainty.

- [x] **Step 1: Write failing boundary tests.**

  Assert an MCP retry sends identical IDs/hash, same-key retries return the same
  run, different payload fails with a machine-readable conflict code, and an old
  internal caller without IDs retains legacy behavior.

- [x] **Step 2: Verify RED, then add caller-generated IDs.**

  Derive a deterministic payload hash from canonical JSON and generate IDs once
  per wave member before the HTTP loop. Never regenerate inside retry handling.

- [x] **Step 3: Make gateway admission coordinator-authoritative.**

  Parse and validate the additive fields, call `submit()` before manager
  execution, and pass the returned run ID as `_preassigned_id`. A duplicate
  applied/claimed command returns its durable response without a second spawn.

- [x] **Step 4: Replace new-caller lost-submission inference.**

  On uncertain HTTP response, query by idempotency key and use the recorded
  decision. Keep the current stuck-wave reconciliation only for legacy callers.

- [x] **Step 5: Route continue, steer, cancel, and release through typed commands.**

  Reuse the same key/hash conflict rules. Do not change tool text or response
  shape except for additive machine-readable identifiers.

- [x] **Step 6: Verify, document, and commit PR 5.**

  Run MCP messaging, admission, scale, and subagent suites. Commit:
  `feat: make subagent commands idempotent`.

---

### Task 6: PR 6 — transactional terminal outbox

**Files:**

- Create: `src/kiro_crew/run_coordinator/delivery.py`
- Modify: `src/kiro_crew/subagent.py`
- Modify: `src/kiro_crew/slack/gateway.py`
- Modify: `docs/system-specs/common/injected-messages.md`
- Modify: `docs/system-specs/modules/messaging.md`
- Modify: `docs/system-specs/modules/subagent.md`
- Create: `test/test_run_coordinator_delivery.py`
- Modify: `test/test_subagent_delivery_ttl_anchor.py`
- Modify: `test/test_subagent_batch_injection.py`
- Modify: `docs/request-for-change/rfc-durable-run-coordinator.md`

**Interfaces:**

- Consumes: `complete`, `claim_outbox`, and `mark_delivered`.
- Produces: `OutboxDeliveryAdapter.drain_once()` and additive `event_id` in the
  subagent completion envelope.

- [x] **Step 1: Write the completion crash-window tests.**

  Inject failures before transaction, after transaction, after destination
  acceptance, and after acknowledgement. Assert every committed terminal has
  one pending/delivered event, repeated drains reuse `event_id`, and no path
  submits a second execution command.

- [x] **Step 2: Verify RED, then implement the delivery adapter.**

  Claim bounded batches, call an injected async destination callback, mark only
  accepted events delivered, release failed claims through their delivery fence
  with persisted retry timing, and never include full `result.txt` in the outbox
  payload.

- [x] **Step 3: Commit terminal outcome and outbox before callbacks.**

  Route the existing final report claim through `coordinator.complete()` before
  gateway delivery. Keep terminal file mirrors and teardown gates; remove only
  in-memory ordering that the durable transaction replaces.

- [x] **Step 4: Add stable event IDs to injected envelopes.**

  Consumers must ignore unknown fields. Queue/digest settlement acknowledges the
  outbox event only when current code would write a delivered tombstone.

- [x] **Step 5: Verify delivery, batch, TTL, and restart tests.**

  Run all named test files with `-n0`, then the broader subagent slice.

- [x] **Step 6: Update message/subagent specs and commit PR 6.**

  Commit: `feat: persist subagent completion delivery`.

---

### Task 7: PR 7 — coordinator-first restart recovery

**Files:**

- Modify: `src/kiro_crew/run_coordinator/legacy.py`
- Create: `src/kiro_crew/run_coordinator/recovery.py`
- Modify: `src/kiro_crew/subagent.py`
- Create: `test/test_run_coordinator_recovery.py`
- Modify: `test/test_subagent_persistence.py`
- Modify: `docs/system-specs/modules/subagent.md`
- Modify: `docs/request-for-change/rfc-durable-run-coordinator.md`

**Interfaces:**

- Consumes: fenced run leases, outbox records, and validated legacy folder
  readers.
- Produces: `RunRecovery.reconcile()` and idempotent `LegacyRunImporter`.

- [x] **Step 1: Write importer and lease-takeover tests.**

  Cover running, completed, delivered, tombstoned, continuable, corrupt, and
  missing-result folders; repeated import; expired lease takeover; stale owner
  mutation rejection; and legacy files left byte-identical.

- [x] **Step 2: Verify RED, then implement the importer.**

  Validate IDs with the existing containment rule, parse known fields only,
  record source version, and create coordinator records idempotently. Corruption
  emits a diagnostic and leaves the folder untouched.

- [x] **Step 3: Write and implement the recovery decision matrix.**

  Coordinator terminal + pending event drains delivery. Running + verified live
  child follows current kill-and-report policy. Starting/running + no verified
  child becomes `interrupted` with partial result retained. No state permits
  automatic replay except the existing zero-tool cancel-recovery rule.

- [x] **Step 4: Add deterministic crash-window tests.**

  Assert convergence after every boundary listed in RFC section 7.3 using
  injected failure points and clocks, never sleeps.

- [x] **Step 5: Add one hermetic subprocess restart test.**

  Spawn only a Python sleeper with `cwd` under `tmp_path`; capture and terminate
  it through `platform_compat`; guarantee cleanup in `finally`; verify takeover
  fences the old owner and preserves partial output.

- [x] **Step 6: Make startup coordinator-first with legacy fallback.**

  Import legacy-only folders once, acquire expired leases, reconcile runs, then
  drain pending delivery. Keep old orphan recovery available when coordinator
  mode is disabled.

- [x] **Step 7: Verify, document, and commit PR 7.**

  Run recovery/persistence/subagent tests, docs lint, formatting, flake8, mypy,
  and the full repository gate if the environment supports it. Commit:
  `feat: recover subagents from the run coordinator`.

---

## Final stacked verification

- [x] Confirm every branch contains exactly one commit relative to its parent.
- [x] Confirm every worktree is clean and no branch was pushed.
- [x] Run `python3 scripts/check_black_formatting.py`.
- [x] Run `isort --check-only` on all changed Python files.
- [x] Run `flake8` and `mypy` on all changed Python files.
- [x] Run all coordinator and subagent-focused tests with `-n0`.
- [x] Run `bash scripts/docs-lint.sh`.
- [x] Run `git diff --check` for every adjacent branch pair.
- [x] Record any unavailable full-suite gate explicitly; never describe an
  unrun gate as passing.

### Local verification record

- The coordinator/subagent slice passed 647 tests serially. The handler, MCP,
  command-authority, and API-contract slice passed another 423 tests serially.
- Formatting, import order, changed-file flake8 and mypy, documentation lint,
  and adjacent-branch whitespace checks passed from the repository virtual
  environment.
- The full repository run was attempted on macOS with Python 3.14: 60,964
  passed, 385 skipped, 6 xfailed, 2 errored, and 95 failed. The failures mixed
  platform/test-harness issues (including BSD `date`, Unix-socket path length,
  and unrelated adversarial parser/timeouts) with stack contract failures. The
  stack-related failures were isolated, corrected, and covered by the two green
  serial slices above; the full-repository attempt is not reported as passing.
