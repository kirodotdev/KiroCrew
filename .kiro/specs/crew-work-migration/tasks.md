# Implementation Plan: Crew-to-Crew Work Migration

**Status:** accepted

Build-stage artifact — the gate reads this file as `plan.md`. No file under `src/`
may be edited until this reaches `accepted`, which itself requires `design.md`
signed-off.

**Scope decision (Task 0, owner-adjudicated 2026-09-01):** proceeding on the plan's
existing assumptions — 0.1 sequenced (cron → session → task-runner), 0.2 standalone
feature (not folded into #4923), 0.3 target-side project rematerialization out of
scope. Owner accepts that a later maintainer ruling may shift slice boundaries but
not the protocol design.

## Overview

Implement one-shot, user-initiated migration of an in-flight session, cron schedule,
or task-runner run from one crew to another over the existing Instances tunnel.

The implementation proceeds **protocol-first, then one unit kind at a time**, in
ascending order of live-state difficulty: shared handoff protocol → cron → session →
task-runner run. Each slice ships independently and is independently useful, which
matches the sequencing question the issue raises for maintainers.

Slices 2, 3 and 4 are each end-to-end (adapter + preflight + CLI + dashboard +
tests) so that merging one does not leave a half-wired surface.

**Blocked on:** issue #7577 is labelled `needs-human`. Task 0 must complete before
any code lands.

## Tasks

- [x] 0. Confirm scope with maintainers before implementation _(owner-adjudicated 2026-09-01)_
  - [x] 0.1 Decision: **sequenced** (cron → session → task-runner)
    - This plan assumes sequenced; a single-drop decision changes slice boundaries but not the design
  - [x] 0.2 Decision: **standalone feature** (not folded into #4923)
    - _Design: Open Design Questions 3_
  - [x] 0.3 Decision: **out of scope** — target-side project rematerialization reported as a requirement, not fixed by migration
    - _Design: Open Design Questions 2_

- [x] 1. Shared migration protocol (no unit kinds yet)
  - [x] 1.1 Add the data model: `MigrationBundle`, `HostRequirement`, `PreflightReport`, `Finding`, `Tombstone`, `CrewRef`, `QuiesceToken`
    - Allow-list serialization helper: a field not explicitly named is dropped
    - _Requirements: 3.4_
  - [x] 1.2 Define the `MigrationUnitAdapter` protocol
    - `describe` / `requirements` / `quiesce` / `unquiesce` / `serialize` / `materialize` / `tombstone`
    - No unit-type knowledge leaks into the coordinator
  - [x] 1.3 Implement `MigrationCoordinator` — the five-step handoff
    - Strict order: preflight → quiesce → transmit → await durable ack → tombstone + release
    - _Requirements: 2.1, 2.2, 2.3, 2.4_
  - [x] 1.4 Implement failure semantics: un-quiesce and retain ownership on every pre-ack failure
    - Distinguish terminal outcome `refused` from `failed`
    - _Requirements: 2.5, 7.1_
  - [x] 1.5 Implement `MigrationReceiver` with two tunnel endpoints on the target
    - `preflight` is pure and read-only; `accept` validates → persists → fsyncs → acks
    - Dedupe `accept` on `handoff_id`
    - _Requirements: 1.1, 2.4, 2.7_
  - [x] 1.6 Implement startup reconciliation for the crash window between ack and tombstone
    - Query target by `handoff_id`; converge to exactly one owner; never resume unconditionally
    - _Requirements: 2.6_
  - [x] 1.7 Implement secret containment: credential-pattern scan over the serialized payload
    - Blocking finding + explicit acknowledgement; no credential material in any bundle
    - _Requirements: 3.1, 3.2, 3.3_
  - [x] 1.8 Implement audit entries on both crews and per-unit duration measurement
    - _Requirements: 3.5, 7.5_
  - [x] 1.9 Wire preflight as a standalone, migration-free check
    - _Requirements: 1.7_
  - [x] 1.10 Protocol tests with a fake receiver
    - Inject failure at each of the five steps; assert the single-owner invariant and source executability
    - Idempotency: same `handoff_id` twice ⇒ one unit on target
    - Crash window: source dies between ack and tombstone ⇒ exactly one owner after reconciliation
    - _Requirements: 2.1–2.7_

- [x] 2. Cron slice (smallest self-contained unit)
  - [x] 2.1 Implement `CronMigrationAdapter` in `src/kiro_crew/cron.py`'s orbit
    - Allow-list the durable `CronJob` fields; drop all four Runtime_Only_Fields; drop dedup/failure-accounting fields; drop `session_key`
    - _Requirements: 4.1, 4.2, 4.3_
  - [x] 2.2 Implement cron preflight probes on the target
    - Script path resolves under the target's crons dir; command permitted by target policy; `agent_id` exists on target (refuse, never fall back to default)
    - _Requirements: 4.4, 4.5, 4.6_
  - [x] 2.3 Implement quiesce: mark non-executing; refuse when a run is in flight
    - _Requirements: 4.9_
  - [x] 2.4 Implement materialize on target: re-bind owning scope, preserve `user_paused`, compute next fire from the job's own `timezone`
    - _Requirements: 4.3, 4.7, 4.8_
  - [x] 2.5 Implement cron tombstone on source — retained, non-executing, names the target
    - _Requirements: 2.8_
  - [x] 2.6 Add `kirocrew cron move <job-id> --to <crew>` and the Schedule-tab action
    - CLI verb + POST /api/crons/{job_id}/move + the Schedule-row "Move to crew…" item, wired in SchedulePage
    - _Requirements: 4.10_
  - [x] 2.7 Cron round-trip and containment tests
    - Every allow-listed field survives; explicit assertion each Runtime_Only_Field is **absent** from the payload
    - **Double-fire test:** after migration, advance a fake clock past the next due instant and assert the source does not fire
    - _Requirements: 4.1, 4.2, 4.7_
  - [x] 2.8 Allow-list drift guard test — fails when a `CronJob` field is added without a migrate/drop decision
    - _Requirements: 3.4_

- [x] 3. Session slice (extends the existing transfer path)
  - [x] 3.1 Add move semantics to `src/kiro_crew/dashboard/session_transfer.py`
    - SessionMigrationAdapter assembles the pieces behind the seam; bundle_builder/importer injected (real wiring pending)
    - _Requirements: 5.1, 5.2_
  - [x] 3.2 Implement session quiesce — block new turns, drain any in-flight turn
    - _Requirements: 5.7_
  - [x] 3.3 Carry the session ledger as durable working state (goal, phase, `next`, tried/rejected)
    - _Requirements: 5.4_
  - [x] 3.4 Handle armed monitor loops: disarm on source at quiesce, re-arm on target after ack, never armed on both
    - _Requirements: 5.8_
  - [x] 3.5 Report dropped non-portable references (`project`, `model`, `workspace`, unmatched `agent`) instead of swallowing them
    - _Requirements: 5.5, 5.6_
  - [x] 3.6 Warn when Layer B is unavailable and the move degrades to transcript-prefix fidelity
    - _Requirements: 5.3_
  - [x] 3.7 Implement session tombstone — transcript retained and readable, displays new home, refuses new turns
    - _Requirements: 5.9, 5.11_
  - [x] 3.8 Add `kirocrew session move <session-id> --to <crew>` and the "Move to crew…" session action
    - CLI verb refuses with exit 2 and names why (a session bundle is only coherent from the live slot); POST /api/chat/slots/{slot}/move plans from the live slot, and SessionActionsMenu offers the action on every surface that renders it
    - _Requirements: 5.10_
  - [x] 3.9 Session migration tests
    - Layer B present ⇒ resume fidelity; Layer B absent ⇒ warning path
    - Assert no monitor loop is armed on both crews after a move
    - Assert the tombstoned source rejects a new turn but still serves its transcript
    - _Requirements: 5.2, 5.3, 5.8, 5.9, 5.11_

- [x] 4. Task-runner slice (largest live state)
  - [x] 4.1 Implement `TaskRunMigrationAdapter` over the `Project` record in `src/kiro_crew/task_models.py`
    - Allow-list task list with per-task status, `current_task`, `WorkingMemory`, `replan_count`, spec content
    - _Requirements: 6.1_
  - [x] 4.2 Implement the `resume` vs `restart` classifier — a **git reproducibility** probe on the target
    - Test `repo_root` resolution, branch reachability, worktree recreatability; name the unreproducible reference
    - _Requirements: 6.2, 6.3_
  - [x] 4.3 Implement quiesce: pause at a task boundary and persist; never serialize a run with a task mid-execution
    - _Requirements: 6.6_
  - [x] 4.4 Require explicit confirmation for a `restart` classification, naming the discarded progress
    - _Requirements: 6.4_
  - [x] 4.5 Implement resume-on-target that does not re-execute tasks already recorded complete
    - _Requirements: 6.5_
  - [x] 4.6 Preserve pending approvals across the move — migration is not an approval channel
    - _Requirements: 6.7_
  - [x] 4.7 Guarantee in-place resumability on any migration failure
    - _Requirements: 6.8_
  - [x] 4.8 Add the task-run move CLI verb and dashboard action
    - `kirocrew taskrun move <task-id> --to <crew> [--runs-file]` off runs.json; POST /api/taskrunner/{task_id}/move plans from the LIVE record (which carries WorkingMemory + current_task); ProjectsPage button on a non-executing run
  - [x] 4.9 Task-run migration tests
    - resume/restart/approval-preserved covered; `Project` allow-list drift guard added
    - _Requirements: 3.4, 6.4, 6.5, 6.7_

- [~] 5. Reversibility, discoverability, and integration _(5.2 partial: Req 7.3 done, Req 7.2 needs a transmit step)_
  - [x] 5.1 Make a migrated unit migratable again from its new owner, including back to the original crew
    - _Requirements: 7.4_
  - [~] 5.2 Surface the unit's target identity on success, and make the tombstone discoverable from the surface that listed the unit before the move
    - Req 7.3 DONE: durable `TombstoneRegistry` (`migration/tombstones.py`), kind-namespaced, fsync'd, degrades to "nothing moved" on a corrupt file, `clear()` on materialize for the move-back case; `kirocrew cron list` prints `↪ migrated to <crew> as <remote id>`
    - Req 7.2 BLOCKED: "shown on success" needs a real success. `MigrationResult.remote_unit_id` carries the identity already, but there are zero production call sites for `migrate()` — no transmit transport exists
    - _Requirements: 7.2, 7.3_
  - [x] 5.3 Two-crew integration tests over a loopback tunnel pair
    - Cron happy path, session happy path, task-run resume, task-run restart-classified, and unreachable target all covered with real components
    - _Requirements: 1.6, 7.1_
  - [x] 5.4 Documentation: add a migration section to the instances module spec and the CLI reference
    - `docs/system-specs/modules/instances.md` §16 (+ ToC entry), sited after §14 session transfer and §15 federated search because both are copies and this is the move; §16.9 states plainly what is NOT wired
    - `docs/system-specs/modules/cli.md` command table: the three move verbs, including why `session move` refuses

## Verification

Per `/home/ec2-user/kirocrew/Makefile` and `/home/ec2-user/kirocrew/conftest.py`:

- Full suite: `make test` (builds first), or `pytest -q` against the built venv
- Targeted: `pytest -q test/test_<name>.py`
- Tests live flat in `/home/ec2-user/kirocrew/test/` under a root `conftest.py`;
  the `writing-tests` skill governs side-effect and residue rules — no writes to the
  real data home, no leaked temp dirs, no cron/thread residue

Every slice must leave `make test` green before the next begins.
