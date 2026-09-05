# Design Document

**Status:** signed-off

Design-stage artifact for `intent.md` (with `requirements.md`). The gate reads this
file as `spec.md`. Reaches `signed-off` only after `intent.md` is `accepted` and the
RFC in `docs/request-for-change/` has been raised.

## Overview

Crew-to-crew migration relocates one in-flight unit of work — a chat session, a
cron schedule, or a task-runner run — from the crew that owns it to another known
crew, over the authenticated Instances tunnel that already carries the crew
selector.

The design's central claim: **this is mostly not new machinery.** Three of the four
hard parts already exist in the tree.

| Capability | Status today | Path |
|---|---|---|
| Authenticated crew↔crew channel | **Exists** | `src/kiro_crew/instances/`, `src/kiro_crew/tunnel/`, `src/kiro_crew/peer_resolve.py` |
| Session serialize + materialize | **Exists as _copy_** | `src/kiro_crew/dashboard/session_transfer.py` |
| Task-run resume from persisted state | **Exists** (crash recovery) | `src/kiro_crew/taskrunner.py`, `src/kiro_crew/task_models.py` |
| Single-owner handoff / tombstone | **Does not exist** | new — this feature's real core |

What the feature actually adds is the **handoff protocol**: quiesce, transmit,
acknowledge, tombstone, release — plus a preflight that refuses early, and cron and
task-run bundle kinds alongside the session one.

### Two corrections to the issue's assumptions

1. **Session migration is not a from-scratch build.** `session_transfer.py` already
   serializes a session into a versioned bundle with *two* layers: Layer A (the
   visible transcript) and Layer B (`bundle_version 2` — the kiro-cli context window
   itself, `<sid>.json` + `<sid>.jsonl`, joined via `session_map.json`). Layer B is
   precisely the "carries enough state that it continues without a manual context
   copy" the issue asks for. Its module docstring is explicit that it is
   *"Copy, never move… Import always allocates a NEW slot key and never touches an
   existing session."* Migration = copy + quiesce + tombstone + ownership release.

2. **Task-run resume is more feasible than the issue assumed.** The issue hedges
   *"if resuming mid-run is infeasible, fall back to restart."* In fact
   `task_models.Project` is a fully serializable record (task list with per-task
   `status`/`attempts`/`result`, `current_task`, `WorkingMemory`, `replan_count`),
   persisted to `runs.json`, and `taskrunner.py` already reconstructs a run after a
   gateway crash via `load_checkpoint` / `build_resume_context`. The blocker is not
   run state — it is **git state**: `repo_root`, `worktree_path`, `branch_name`,
   `commit_hashes`. So the `resume` vs `restart` decision is a *git reproducibility*
   question, and that is what the preflight should actually test.

## Architecture

```
Source Crew                                          Target Crew
───────────                                          ───────────
MigrationCoordinator
  │
  ├─1─ preflight ──────── Instances tunnel ────────►  MigrationReceiver.preflight()
  │    (read-only)      ◄──── PreflightReport ─────      capability + reference probe
  │
  ├─2─ quiesce()  ← UnitAdapter (per kind)
  │      cron:    mark non-executing, refuse if mid-run
  │      session: block new turns, drain in-flight turn
  │      taskrun: pause at task boundary, persist
  │
  ├─3─ serialize() → MigrationBundle{bundle_kind, bundle_version, handoff_id, payload}
  │                    ──────────────────────────►  MigrationReceiver.accept()
  │                                                    validate → persist → fsync
  │                  ◄────── AcceptAck{unit_id} ───     (idempotent on handoff_id)
  │
  ├─4─ write Tombstone{target_crew, remote_unit_id, ts}
  └─5─ release ownership → target may execute
```

Ownership is a **release-after-ack** protocol, not a distributed lock. There is no
consensus layer: the source is authoritative until it durably records that the
target has the unit. That yields at-most-one executor at every instant, and on any
failure short of step 4 the source simply un-quiesces.

## Components and Interfaces

### `MigrationUnitAdapter` (new, one per kind)

The seam that keeps the protocol generic. Each adapter owns the knowledge of what
its unit type's durable state is and how to stop it safely.

```python
class MigrationUnitAdapter(Protocol):
    bundle_kind: str          # "cron" | "session" | "taskrun"
    bundle_version: int

    async def describe(self, unit_id: str) -> UnitDescriptor: ...
    async def requirements(self, unit_id: str) -> list[HostRequirement]: ...
    async def quiesce(self, unit_id: str) -> QuiesceToken: ...
    async def unquiesce(self, unit_id: str, token: QuiesceToken) -> None: ...
    async def serialize(self, unit_id: str) -> dict: ...
    async def materialize(self, payload: dict) -> str: ...   # returns local unit id
    async def tombstone(self, unit_id: str, target: CrewRef, remote_id: str) -> None: ...
```

`HostRequirement` is the containment primitive for Requirement 3: a *named
requirement* (`kind`, `identity`, `severity`) rather than a transferred value.
Kinds: `credential`, `mcp_server`, `agent`, `project_checkout`, `script_path`,
`command_policy`, `git_repo`.

### `MigrationCoordinator` (new, source side)

Drives the five steps, owns the failure semantics, emits the audit entries. Holds
no unit-type knowledge — everything type-specific is behind the adapter.

### `MigrationReceiver` (new, target side)

Two tunnel endpoints: `preflight` (pure, read-only) and `accept` (validate,
persist, fsync, ack). `accept` dedupes on `handoff_id` so a retransmit cannot
create a second unit (Requirement 2.7).

### Serialization: allow-list, not exclude-list

Requirement 3.4 forces a specific choice. Every adapter declares the fields it
ships; anything not named is dropped. This matters most for `CronJob`, whose
docstring already documents four fields as *"Runtime-only (never serialized)"* —
`fire_time_denied`, `run_never_started`, `result_produced`, `failure_recorded`.
An exclude-list would ship the fifth such field somebody adds next year; an
allow-list will not.

## Per-Unit Design

### Cron (first slice — smallest, self-contained)

`CronJob` in `src/kiro_crew/cron.py` is a single dataclass persisted to
`~/.kiro/crew/crons.json` under advisory file locking. That makes it the cleanest
first slice, matching the issue's own guess.

- **Ships:** `name`, `message`, `schedule`, `script`/`command`, `timezone`,
  `skip_dates`, `timeout`, `timeout_secs`, `approval_mode`, `agent_id`, `silent`,
  `minimal_context`, `persistent_session`, `context_enabled`, `channel`,
  `thread_ts`, `delete_after_run`, `user_paused`, `hide_in_chat`, `model`.
- **Dropped:** all four Runtime_Only_Fields; every dedup/failure-accounting field
  (`last_posted_hash`, `consecutive_failures`, `last_failure_at`, …) — these are
  observations of the *source host's* execution history and are meaningless on the
  target; `session_key`, which is a source-local ownership scope the target must
  re-bind (Requirement 4.3).
- **Preflight probes:** script path resolves under the target's crons dir;
  command permitted by target policy; `agent_id` exists on target (refuse rather
  than let the target silently fall back to its default agent — Requirement 4.6).
- **Quiesce:** set non-executing; refuse if a run is in flight.
- **Next fire** is computed by the target from the schedule, so a job crossing a
  timezone boundary fires per its own `timezone` field, not the target's locale.

### Session (second slice — extends an existing path)

Reuses `build_transfer_bundle_async` / `api_chat_slot_import` in
`src/kiro_crew/dashboard/session_transfer.py`. The additions are narrow:

- A `move` flag on the bundle, and a source-side quiesce so no turn is in flight.
- Tombstone the source slot: retained and readable (Requirement 5.11), displaying
  its new home, refusing new turns.
- Carry the session ledger (`src/kiro_crew/session_ledger.py`) as durable working
  state — goal, phase, `next`, tried/rejected approaches. This is what makes a
  cold resume on the target coherent.
- **Monitor loops** are the sharp edge (Requirement 5.8): an armed loop on both
  crews would double-fire. Disarm on source at quiesce, re-arm on target after ack.
- Inherit the existing non-portability rules verbatim — `project`, `model`,
  `workspace` dropped, `agent` as hint-only — and *report* the drops rather than
  swallowing them (Requirement 5.6). The docstring's reasoning already holds for
  migration: a Mac worktree path does not exist on a Linux EC2 host.

### Task-runner run (third slice — largest live state)

- Serialize the `Project` record from `runs.json`; pause at a task boundary first.
- Preflight classifies `resume` vs `restart` by testing git reproducibility on the
  target: does `repo_root` resolve, is the branch reachable, can the worktree be
  recreated. This is the real determinant, not the run record.
- `restart` requires explicit confirmation naming the discarded progress
  (Requirement 6.4) — never silent.
- A task in `requires_approval` arrives still awaiting approval (Requirement 6.7):
  migration is not an approval channel.

## Data Model

```python
@dataclass
class MigrationBundle:
    bundle_kind: str        # "cron" | "session" | "taskrun"
    bundle_version: int
    handoff_id: str         # idempotency key
    created_ts: float
    source_crew: CrewRef
    payload: dict
    requirements: list[HostRequirement]

@dataclass
class PreflightReport:
    findings: list[Finding]           # severity: "blocking" | "advisory"
    resume_class: str | None          # taskrun only: "resume" | "restart"
    @property
    def blocked(self) -> bool: ...

@dataclass
class Tombstone:
    unit_kind: str
    target_crew: CrewRef
    remote_unit_id: str
    migrated_ts: float
```

## Error Handling

| Failure point | Behaviour |
|---|---|
| Preflight blocking finding | Refuse. Nothing quiesced. Outcome `refused`, distinct from `failed` (R7.1). |
| Target unreachable | Refuse at preflight; source untouched (R1.6). |
| Transmit fails / no ack | Un-quiesce, retain ownership, no tombstone (R2.5). |
| Target rejects bundle | Same as above; surface the target's reason. |
| Source crash after ack, before tombstone | Startup reconciliation queries the target for `handoff_id`; if the target holds it, finish the tombstone; else un-quiesce (R2.6). |
| Retransmit of same `handoff_id` | Target returns the existing unit id; no duplicate (R2.7). |
| Unit mid-execution at request | Refuse with "mid-run" (R4.9, R6.6). |

The invariant worth stating plainly: **every failure mode short of a durable ack
leaves the source owning the work.** The design biases toward "migration did not
happen" over "migration half-happened".

## Testing Strategy

Tests live in `/home/ec2-user/kirocrew/test/` (flat, ~1880 files) with a root
`conftest.py`; run via `make test` (which builds first) or `pytest -q`. The
`writing-tests` skill governs side-effect and residue rules — no writes to the real
data home, no leaked temp dirs, no cron/thread residue.

1. **Adapter unit tests, per kind.** Serialize → materialize round-trip preserves
   every allow-listed field. Explicit assertion that each Runtime_Only_Field is
   *absent* from the payload — a regression guard that survives new fields.
2. **Allow-list drift guard.** A test that fails when a field is added to `CronJob`
   or `Project` without an explicit migrate/drop decision. This is the test that
   keeps Requirement 3.4 true over time rather than at merge time only.
3. **Protocol state-machine tests** with a fake receiver: inject failure at each of
   the five steps, assert the ownership invariant holds and the source is left
   executable in every non-terminal case.
4. **Idempotency test.** Same `handoff_id` twice ⇒ one unit on target.
5. **Crash-window test.** Simulate source death between ack and tombstone; assert
   reconciliation converges to exactly one owner.
6. **Double-fire test** (the one that matters most for cron): after a completed
   migration, advance a fake clock past the next due instant and assert the source
   does not fire.
7. **Secret containment test.** A unit whose definition embeds a value matching the
   repo's credential patterns produces a `blocking` finding; assert no credential
   material appears in a serialized bundle.
8. **Two-crew integration test** over a loopback tunnel pair, per unit kind, for the
   happy path and for a `restart`-classified task run.

## Open Design Questions

1. **Reconciliation authority.** Startup reconciliation assumes the target can be
   queried by `handoff_id`. That implies the target retains handoff records for
   some window — how long, and is that window a config knob?
2. **Project rematerialization.** Requirements treat a missing project checkout as
   a reported requirement, not something migration fixes. Cloning on the target is
   plausibly a follow-up feature rather than part of this one.
3. **Framing against #4923.** If maintainers prefer, sessions here could be
   implemented as a generalization of #4923's target from "Kiro Cloud" to "any known
   crew". That is a sequencing decision, not a design difference — the adapter seam
   accommodates either.
