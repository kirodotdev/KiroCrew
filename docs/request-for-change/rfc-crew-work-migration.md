---
title: Crew-to-Crew Work Migration — single-owner handoff for sessions, schedules, and task runs
status: draft
revision: v1
author: timwukp, with Kiro
created: 2026-09-02
last-audited: 2026-09-02
audited-at: ebc0936
doc-pr:
implementation-prs: []
tracking-issues: [7577]
supersedes: []
superseded-by: []
---
# RFC: Crew-to-Crew Work Migration — single-owner handoff for sessions, schedules, and task runs

- Status: draft — nothing in this document is on main. Verified at `ebc0936`:
  `git ls-tree -r --name-only main | grep -c 'kiro_crew/migration'` returns `0`.
- Author: timwukp, with Kiro
- Created: 2026-09-02
- Audited against: `ebc0936`
- Related: `src/kiro_crew/docs/system-specs/modules/instances.md`,
  `src/kiro_crew/docs/system-specs/modules/session.md`, and
  `docs/request-for-change/rfc-durable-run-coordinator.md`
- Tracking issue: [#7577](https://github.com/kirodotdev/KiroCrew/issues/7577)

## 1. Summary

Add a one-shot, user-initiated **migration** of a single in-flight unit of work —
a chat session, a cron schedule, or a task-runner run — from the crew that owns
it to another known crew, over the authenticated tunnel that already carries the
crew selector.

The central claim: **this is mostly not new machinery.** Three of the four hard
parts already exist in the tree. What the feature actually adds is a **handoff
protocol** with one invariant:

> Every failure mode short of a durable acknowledgement leaves the **source**
> owning the work.

Ownership is a *release-after-ack* protocol, not a distributed lock. There is no
consensus layer: the source stays authoritative until it durably records that the
target holds the unit. That yields at-most-one executor at every instant, and on
any failure before that record the source simply un-quiesces.

This RFC adopts the vocabulary of `rfc-durable-run-coordinator.md` (leases,
fencing, idempotent commands) but declares a **non-blocking** relationship to it:
that RFC is `draft` with zero implementation and is scoped to subagent runs.
Coupling this feature to it would block #7577 indefinitely; adopting its
vocabulary lets the two converge later without either gating the other.

## 2. Motivation and current state

Verified at `ebc0936` on 2026-09-02.

### 2.1 What already exists

| Capability | Status today | Path |
|---|---|---|
| Authenticated crew↔crew channel | **Exists** | `src/kiro_crew/instances/registry.py`, `instances/ssh_tunnel_manager.py`, `instances/token_mint.py`, `src/kiro_crew/tunnel/`, `src/kiro_crew/peer_resolve.py` |
| Session serialize + materialize | **Exists as _copy_** | `src/kiro_crew/dashboard/session_transfer.py` (1361 lines) |
| Task-run resume from persisted state | **Exists**, at reduced fidelity | `src/kiro_crew/task_reporter.py:155` `load_checkpoint`, `:179` `build_resume_context`; `src/kiro_crew/taskrunner.py:1825` `_RUNS_FILE = "runs.json"`, `:1965` `_load_runs` (called from `:335`). `runs.json` omits `WorkingMemory` and `current_task` — see §2.2 |
| Single-owner handoff / tombstone | **Does not exist** | new — this feature's real core |

### 2.2 Two corrections to the issue's assumptions

The issue estimates session migration as a from-scratch build and hedges that
resuming a task run mid-flight may be infeasible. Both are wrong in the same
direction — the work is smaller than assumed, but concentrated in a different
place.

**Session migration is not a from-scratch build.** `session_transfer.py` already
serializes a session into a versioned bundle with *two* layers: Layer A (the
visible transcript) and Layer B (`bundle_version 2` — the kiro-cli context window
itself). Layer B is precisely the "carries enough state that it continues without
a manual context copy" the issue asks for. The module docstring is explicit at
`src/kiro_crew/dashboard/session_transfer.py:23`: *"**Copy, never move.** Import
always allocates a NEW slot key and never touches an existing session."*
Migration therefore = copy + quiesce + tombstone + ownership release.

**Task-run resume is more feasible than assumed.** `task_models.py:93` `Project`
is a serializable record (task list with per-task status/attempts/result,
`current_task`, `WorkingMemory`, `replan_count`), and the runner already
reconstructs a run after a crash. The blocker is not run state — it is **git
state**: `repo_root`, `worktree_path`, `branch_name`, `commit_hashes`. So
`resume` vs `restart` is a *git reproducibility* question, and that is what
preflight should test.

One caveat, verified against `taskrunner.py`'s `_serialize_runs` at `ebc0936`:
`runs.json` is a **subset** of `Project`, not a mirror of it. It stores the task
list under `task_details` with per-task status, but it carries **no
`WorkingMemory` and no `current_task`** — those exist only on the in-memory
record. A migration sourced from the live runner is therefore higher fidelity
than one sourced from disk, and the disk path must *report* the gap rather than
arrive quietly without it. This is the same honesty rule as the Layer B warning
in §5.5.

### 2.3 The problem

Today a user who wants work to continue on a different crew has three bad
options: leave it where it is, copy the session and manually stop the original
(a double-execution window), or abandon and re-create it (losing accumulated
state). For a cron schedule there is no copy path at all, and for a task run the
only recovery is a restart that discards completed tasks.

The dangerous case is a **cron schedule copied but not stopped**: both crews then
fire it. Nothing in the tree prevents that today, because there is no notion of
releasing ownership.

## 3. Goals

1. One-shot, **user-initiated** migration of one unit to one named crew.
2. **At-most-one executor** at every instant, including across a source crash.
3. Preflight that **refuses early** and names what the target cannot satisfy.
4. A **tombstone** on the source: retained, readable, non-executing, naming its
   new home.
5. **Reversibility** — a migrated unit is migratable again, including back.
6. **Credential containment**: no credential material crosses the boundary.
7. Reuse the existing bundle formats and transport; define no second wire format.

## 4. Non-goals

- **Distributed scheduling or HA.** This is a user-initiated move between two
  known crews, not a scheduler that places work automatically.
- **Multi-owner or replicated execution.** The invariant is single-owner.
- **Project rematerialization on the target.** A missing checkout is *reported as
  a requirement*, not fixed by migration. Cloning on the target is plausibly a
  follow-up feature (§10).
- **Kiro Cloud chat resume ([#4923](https://github.com/kirodotdev/KiroCrew/issues/4923)).**
  Related but distinct; see §9.3.
- **Migrating more than one unit per operation.** Batch is not in scope.

## 5. Design

### 5.1 The five steps

```
Source Crew                                          Target Crew
───────────                                          ───────────
MigrationCoordinator
  │
  ├─1─ preflight ──────── authenticated tunnel ────►  MigrationReceiver.preflight()
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

The order is not negotiable. Preflight is read-only and happens **before**
anything is quiesced, so a refusal costs the user nothing. Quiesce happens
**before** serialize, so no bundle is ever taken from a moving unit. The
tombstone is written only **after** a durable ack.

### 5.2 `MigrationUnitAdapter` — the seam

One adapter per unit kind owns the knowledge of what its type's durable state is
and how to stop it safely. The coordinator holds none of it.

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

`HostRequirement` is the containment primitive: a **named requirement**
(`kind`, `identity`, `severity`) rather than a transferred value. Kinds:
`credential`, `mcp_server`, `agent`, `project_checkout`, `script_path`,
`command_policy`, `git_repo`. The target is asked *"do you have X?"*, never
handed X.

### 5.3 Serialization: allow-list, not exclude-list

Every adapter declares the fields it ships; anything not named is dropped. This
matters most for `CronJob` (`src/kiro_crew/cron.py:506`), whose own comments
already mark four fields as *"Runtime-only (never serialized)"* at `cron.py:525`,
`:534`, `:545` and one more below. An exclude-list would ship the fifth such
field somebody adds next year; an allow-list will not. A drift-guard test fails
when a field is added without an explicit migrate/drop decision.

### 5.4 Data model

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

### 5.5 Per-unit design

**Cron** (first slice — smallest, self-contained). `CronJob` is a single
dataclass under advisory file locking, which makes it the cleanest first slice.
Ships the durable schedule/delivery fields; drops the four runtime-only fields,
every dedup/failure-accounting field (they are observations of the *source
host's* execution history and are meaningless on the target), `session_key` (a
source-local ownership scope the target re-binds), and `id` (the target allocates
its own). Preflight probes: script path resolves under the target's crons dir;
command permitted by target policy; `agent_id` exists on target — **refuse rather
than let the target silently fall back to its default agent**. Next fire is
computed by the target from the schedule and the job's own `timezone`, not the
target's locale.

**Session** (second slice — extends an existing path). Reuses the Layer A / Layer
B bundle from `session_transfer.py`; adds a source-side quiesce, and a tombstone
that keeps the slot readable while refusing new turns. Two sharp edges:

- **Monitor loops.** An armed loop on both crews would double-fire. Disarm on
  source at quiesce; arm on target only after ack; never both.
- **Non-portable references.** Inherit the existing rules verbatim — `project`,
  `model`, `workspace` dropped, `agent` hint-only — and *report* the drops rather
  than swallowing them. A Mac worktree path does not exist on a Linux EC2 host.
  When Layer B is unavailable the move degrades to transcript-prefix fidelity and
  says so.

**Task-runner run** (third slice — largest live state). Pause at a task boundary
before serializing; never serialize a run with a task mid-execution. Preflight
classifies `resume` vs `restart` by testing git reproducibility on the target.
`restart` requires explicit confirmation naming the discarded progress — never
silent. A task in `requires_approval` arrives still awaiting approval: migration
is not an approval channel.

### 5.6 Error handling

| Failure point | Behaviour |
|---|---|
| Preflight blocking finding | Refuse. Nothing quiesced. Outcome `refused`, distinct from `failed`. |
| Target unreachable | Refuse at preflight; source untouched. |
| Transmit fails / no ack | Un-quiesce, retain ownership, no tombstone. |
| Target rejects bundle | Same, surfacing the target's reason. |
| Source crash after ack, before tombstone | Startup reconciliation queries the target for `handoff_id`; if the target holds it, finish the tombstone; else un-quiesce. Never resume unconditionally. |
| Retransmit of same `handoff_id` | Target returns the existing unit id; no duplicate. |
| Unit mid-execution at request | Refuse with "mid-run". |

The design biases toward *"migration did not happen"* over *"migration
half-happened"*.

## 6. Migration plan

Protocol first, then one unit kind at a time in ascending order of live-state
difficulty. Each phase is independently shippable and independently abandonable.

**Phase 0 — this document.** Blocked on: #7577 is labelled `needs-human`.
Exit criteria: maintainers rule on (a) all three unit kinds together vs
sequenced, (b) framing against #4923 (§9.3), (c) project rematerialization out of
scope.

**Phase 1 — shared protocol, no unit kinds.** Data model, allow-list serializer,
adapter Protocol, `MigrationCoordinator` (five steps + failure semantics),
`MigrationReceiver` (`preflight` pure; `accept` validate→persist→fsync→ack,
idempotent on `handoff_id`; `lookup` for reconciliation), startup reconciliation,
credential scan, audit entries on both crews.
Exit criteria: a fake-receiver test injects failure at each of the five steps and
asserts the source is left executable in every non-terminal case; the same
`handoff_id` twice yields one unit on the target; a simulated source death
between ack and tombstone converges to exactly one owner.

**Phase 2 — cron slice, end to end.** Adapter + preflight probes + `kirocrew cron
move <job-id> --to <crew>` + Schedule-tab action + tests.
Exit criteria: every allow-listed field survives a round trip and each
runtime-only field is asserted **absent** from the payload; the drift guard fails
on an undeclared new `CronJob` field; and the **double-fire test** — after a
completed migration, advance a fake clock past the next due instant and assert
the source does not fire.

**Phase 3 — session slice.** Move semantics over the existing bundle, quiesce,
ledger carry, monitor disarm/re-arm, non-portability reporting, Layer-B-absent
warning, tombstone, CLI + session action.
Exit criteria: Layer B present ⇒ resume fidelity, absent ⇒ warning path; no
monitor loop armed on both crews after a move; the tombstoned source refuses a
new turn but still serves its transcript.

**Phase 4 — task-run slice.** `Project` adapter, resume/restart classifier,
task-boundary quiesce, restart confirmation, resume that skips completed tasks,
approval preservation, in-place resumability on failure, CLI + action.
Exit criteria: no completed task re-executes on the resume path; the restart path
requires confirmation and is never silent; a `requires_approval` task still
awaits approval on the target; `Project` has a drift guard.

**Phase 5 — reversibility, discoverability, docs.** Re-migration including back
to the origin; target identity surfaced on success and the tombstone discoverable
from the surface that listed the unit; two-crew integration tests over a loopback
tunnel pair; a migration section in the instances module spec and the CLI
reference.
Exit criteria: an A→B→A round trip leaves exactly one owner at every hop; the
integration suite covers the happy path per unit kind, a `restart`-classified
task run, and an unreachable target.

## 7. Backward compatibility

Additive. No existing endpoint changes behaviour and no existing artifact format
changes:

- `session_transfer.py`'s copy semantics stay exactly as they are. Migration adds
  a `move` path beside them; a plain transfer still allocates a new slot and
  touches nothing.
- `CronJob` gains no fields. The allow-list lives in the adapter, not on the
  dataclass.
- A crew without the feature is never a migration target: preflight is a new
  endpoint, so an old target fails the reachability check and the move is refused
  with the source untouched.
- Tombstones are new state on the source. A downgrade leaves them as ordinary
  disabled units — inert, not corrupt.

## 8. Security considerations

- **No credential material crosses the boundary.** `HostRequirement` names a
  requirement; it never carries a value. A credential-shaped payload produces a
  **blocking** preflight finding, and the finding text names the matched pattern,
  never the matched value.
- **No new inbound surface.** Both endpoints ride the existing authenticated
  crew↔crew tunnel and its token mint (`instances/token_mint.py`); this RFC adds
  no listener and no new auth path.
- **`accept` is the only mutating endpoint** and is idempotent on `handoff_id`,
  so a replayed bundle cannot create a second unit.
- **Fail closed on requirement checks.** A preflight with no requirement probe
  supplied must not silently pass: an unknown agent, an unresolvable script path,
  and a command with no policy verdict are all refusals, not defaults. This is a
  deliberate ergonomic cost — see §10.4.
- **Audit on both crews.** The move is a permission-relevant transfer of the
  right to execute, so both the releasing and the accepting crew record it, with
  the `handoff_id` as the correlation key.
- **The tombstone is not a delete.** The source transcript stays readable, so
  migration never destroys the user's record of the work.

## 9. Alternatives considered

**9.1 A distributed coordinator with leases and fencing.** This is what
`rfc-durable-run-coordinator.md` proposes for subagent runs. Rejected *for this
feature*: a one-shot, user-initiated move between two known crews does not need a
consensus layer, and building one would make #7577 depend on a `draft` RFC with
zero implementation. Release-after-ack gives the same at-most-one guarantee for
this narrower problem. The vocabulary is adopted so the two can converge later.

**9.2 Copy, then have the user stop the original.** This is what the tree allows
today. Rejected: it leaves a double-execution window whose length is however long
the user takes, and for cron that means both crews firing. Making the release
automatic and ack-gated is the entire point.

**9.3 Implement session migration as a generalization of #4923.** #4923 resumes a
chat in Kiro Cloud; generalizing its target from "Kiro Cloud" to "any known crew"
would cover this RFC's session slice. This is a *sequencing* decision, not a
design difference — the adapter seam accommodates either. Left open at §10.3.

**9.4 Exclude-list serialization.** Rejected: it ships the next field somebody
adds. The allow-list plus a drift-guard test keeps the decision explicit over
time rather than only at merge time.

**9.5 Migrate by replaying the source's inputs on the target.** Rejected: a
session's model context is not reconstructible from its visible transcript, which
is exactly why Layer B exists.

## 10. Open questions

**10.1 Reconciliation authority and retention.** Startup reconciliation assumes
the target can be queried by `handoff_id`, which implies the target retains
handoff records for some window. How long, and is that window a config knob? A
window shorter than a plausible source outage turns a recoverable crash into an
ambiguous one.

**10.2 Project rematerialization.** Requirements treat a missing project checkout
as a reported requirement. Should a follow-up feature clone it on the target, and
does that belong to this feature or to `rfc-crew-projects.md`?

**10.3 Framing against #4923.** Standalone feature, or session migration as a
generalization of #4923? Maintainer call; affects phase boundaries, not the
design.

**10.4 Does a migrated session keep any project binding?** The existing transfer
path deliberately drops `project` and leaves the imported slot unbound. Migration
inherits that, which means a moved session arrives without a working directory
even when the target *does* have an equivalent checkout. Reporting the drop is
the floor; whether the target should offer to re-bind is unresolved.

**10.5 Should preflight be exposed on its own?** A "can this crew take this
unit?" check with no migration attached is useful for capacity planning and is
already pure and read-only. Exposing it as a first-class verb is cheap but widens
the API surface.
