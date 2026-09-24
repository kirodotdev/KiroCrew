# Requirements Document

**Status:** signed-off

Part of the Design-stage artifact for `intent.md` (with `design.md`). Blocked from
sign-off until `intent.md` reaches `accepted`.

## Introduction

A crew's chat sessions, task-runner runs, and cron schedules are bound to the crew
that created them and execute on that crew's host. There is no supported way to
relocate an **in-flight** unit of work to a different crew, so a user with a 24/7
remote crew on their own EC2 must manually reconstruct work on the target when they
close their laptop — re-pasting context, re-creating cron jobs by hand, and
restarting task-runner runs from spec with progress lost.

This feature adds a one-shot, user-initiated **migration** of an existing unit of
work from the crew that owns it to another known crew, over the authenticated
Instances tunnel that already powers the crew selector. It is deliberately narrower
than #3278 (auto-routing new work to remote compute) and broader than #4923
(cross-device resume of Kiro Cloud chat sessions only).

Source: [issue #7577](https://github.com/kirodotdev/KiroCrew/issues/7577).

## Glossary

- **Crew**: One Kiro Crew instance with its own host, config home, and data home. Registered locally and listed under "Crews you can switch to".
- **Migration_Unit**: One relocatable piece of work — a `Session_Unit`, a `Cron_Unit`, or a `TaskRun_Unit`.
- **Source_Crew**: The crew that currently owns the Migration_Unit and initiates the move.
- **Target_Crew**: The crew that receives the Migration_Unit.
- **Migration_Bundle**: The versioned, serialized wire form of a Migration_Unit, carrying a `bundle_kind` discriminator and a `bundle_version`.
- **Preflight**: A read-only capability negotiation with the Target_Crew that reports what the target lacks before any state is committed or the source is quiesced.
- **Tombstone**: A retained, non-executing record on the Source_Crew that names the Target_Crew and the new unit id, so history is not silently orphaned.
- **Quiesce**: Bringing a Migration_Unit to a stable non-executing state on the Source_Crew so no run can advance during the handoff.
- **Ownership_Token**: The single-writer marker that establishes which crew may execute a Migration_Unit; exactly one crew holds it at any instant.
- **Session_Transfer**: The existing copy-based session export/import in `dashboard/session_transfer.py` — `build_transfer_bundle_async` on the sender, `api_chat_slot_import` on the receiver. Carries Layer A (visible transcript) and optionally Layer B (the kiro-cli context window, `<sid>.json` + `<sid>.jsonl`, joined via `session_map.json`).
- **Instances_Tunnel**: The authenticated peer channel in `instances/` and `tunnel/`, resolved via `peer_resolve.py`, already used by the crew selector and `kirocrew cloud connect`.
- **Runtime_Only_Field**: A `CronJob` attribute documented as never serialized (`fire_time_denied`, `run_never_started`, `result_produced`, `failure_recorded`).
- **Host_Local_Reference**: State whose value is only meaningful on the Source_Crew's host — a filesystem path, a git worktree, a credential, an MCP server binding, a project checkout.

## Requirements

### Requirement 1: Unified Migration Preflight

**User Story:** As a user about to move work to another crew, I want to be told what the target cannot support *before* anything moves, so that I never end up with work stranded or silently broken on arrival.

#### Acceptance Criteria

1. THE Preflight SHALL execute against the Target_Crew over the Instances_Tunnel and SHALL NOT mutate state on either crew.
2. THE Preflight SHALL return a structured report enumerating, per finding: a severity of `blocking` or `advisory`, a machine-readable code, and a human-readable remediation.
3. WHEN the Target_Crew does not support the Migration_Bundle's `bundle_kind` or `bundle_version`, THEN THE Preflight SHALL return a `blocking` finding and THE migration SHALL be refused.
4. WHEN a Migration_Unit references a Host_Local_Reference that is absent on the Target_Crew, THEN THE Preflight SHALL report it with the reference's kind and identity.
5. THE Preflight SHALL classify a missing credential, a missing MCP server, and a missing project checkout as distinct finding codes.
6. IF the Target_Crew is unreachable or fails authentication, THEN THE Preflight SHALL return a `blocking` finding naming the transport failure and THE Source_Crew SHALL retain ownership unchanged.
7. THE Preflight SHALL be invocable independently of a migration, so a user can check portability without committing to a move.

### Requirement 2: Single-Owner Handoff Protocol

**User Story:** As a user moving a schedule, I want a guarantee that it never fires on two crews at once and is never lost in transit, so that migration is safe to run on live work.

#### Acceptance Criteria

1. THE Ownership_Token for a Migration_Unit SHALL be held by exactly one crew at any instant.
2. THE handoff SHALL proceed in the strict order: Preflight, Quiesce on Source_Crew, transmit Migration_Bundle, Target_Crew acknowledges durable persistence, Source_Crew writes Tombstone and releases Ownership_Token.
3. WHILE a Migration_Unit is quiesced and the handoff is incomplete, THE Source_Crew SHALL NOT start any new execution of that unit.
4. WHEN the Target_Crew has not acknowledged durable persistence, THE Target_Crew SHALL NOT begin executing the Migration_Unit.
5. IF transmission fails, the acknowledgement is not received, or the Target_Crew rejects the Migration_Bundle, THEN THE Source_Crew SHALL un-quiesce the Migration_Unit, retain the Ownership_Token, and write no Tombstone.
6. IF the Source_Crew crashes after the Target_Crew acknowledged but before the Tombstone is written, THEN on restart THE Source_Crew SHALL detect the outstanding handoff and resolve it to exactly one owner rather than resuming execution unconditionally.
7. THE migration SHALL be idempotent under retry of the same handoff id: a retransmitted Migration_Bundle SHALL NOT create a second unit on the Target_Crew.
8. THE Tombstone SHALL record the Target_Crew identity, the unit id on the target, and the handoff timestamp, and SHALL be non-executing.

### Requirement 3: Secret and Host-Local Reference Containment

**User Story:** As a security-conscious user, I want migration to never quietly ship my machine's credentials or paths to another host, so that moving work does not widen my secret exposure.

#### Acceptance Criteria

1. THE Migration_Bundle SHALL NOT contain credential material, API tokens, or the contents of credential files.
2. THE Migration_Bundle SHALL represent every Host_Local_Reference as a named requirement to be satisfied on the Target_Crew, not as a literal transferred value.
3. WHEN a Migration_Unit's definition embeds a value matching the repository's credential-detection patterns, THEN THE migration SHALL surface it as a `blocking` Preflight finding and SHALL require explicit user acknowledgement before proceeding.
4. THE serialization SHALL operate on an explicit allow-list of fields, so that a field added to a unit's record in future is excluded by default rather than shipped by default.
5. THE migration SHALL record an audit entry on both crews naming the unit, the peer crew, the initiating user, and the outcome.

### Requirement 4: Cron Schedule Migration

**User Story:** As a user with a schedule on my laptop crew, I want to move that job to my always-on remote crew so it keeps firing on the same cadence after I close the laptop.

#### Acceptance Criteria

1. THE Cron_Unit Migration_Bundle SHALL carry the durable fields of the `CronJob` record, including its schedule, `message`, `script`, `command`, `timezone`, `skip_dates`, `timeout`, `approval_mode`, `agent_id`, and delivery routing.
2. THE Cron_Unit Migration_Bundle SHALL exclude every Runtime_Only_Field.
3. THE Cron_Unit Migration_Bundle SHALL exclude `session_key`, and THE Target_Crew SHALL bind the arriving job to its own owning scope.
4. WHEN the Cron_Unit is a script cron, THEN THE Preflight SHALL verify the script path resolves under the Target_Crew's crons directory and SHALL return a `blocking` finding when it does not.
5. WHEN the Cron_Unit is a command cron, THEN THE Preflight SHALL verify the command is permitted by the Target_Crew's policy and SHALL return a `blocking` finding when it is not.
6. WHEN the Cron_Unit references an agent name absent on the Target_Crew, THEN THE Preflight SHALL return a `blocking` finding rather than allowing the target to fall back to a default agent.
7. WHEN the migration completes, THE Source_Crew SHALL leave the job non-executing and THE next fire SHALL occur on the Target_Crew at the schedule's next due instant.
8. THE Target_Crew SHALL preserve the job's paused state: a `user_paused` job SHALL arrive paused.
9. THE Source_Crew SHALL refuse to migrate a Cron_Unit whose run is currently executing, and SHALL report that the job is mid-run.
10. THE migration SHALL be exposed as `kirocrew cron move <job-id> --to <crew>` and as an action in the dashboard Schedule tab.

### Requirement 5: Session Migration

**User Story:** As a user mid-conversation on my laptop, I want to move that session to my remote crew and pick it up there with its context intact, so I do not re-paste the conversation.

#### Acceptance Criteria

1. THE Session_Unit migration SHALL reuse the existing Session_Transfer bundle format and its Layer A / Layer B structure rather than defining a second session wire format.
2. WHEN the session has an associated kiro-cli context window, THEN THE Migration_Bundle SHALL carry Layer B so the session resumes with full fidelity rather than replaying a lossy transcript prefix.
3. WHEN Layer B is unavailable, THEN THE migration SHALL proceed with Layer A and SHALL warn the user that resume fidelity is reduced.
4. THE Session_Unit Migration_Bundle SHALL carry the session's durable working state, including its ledger record and its pending next step.
5. THE Session_Unit migration SHALL apply the Session_Transfer rules for non-portable metadata: `project`, `model`, and `workspace` SHALL NOT be carried, and `agent` SHALL be carried as a hint applied only when the Target_Crew has an agent of that name.
6. WHEN a non-portable reference is dropped, THEN THE migration SHALL report which references were dropped so the user can re-establish them on the Target_Crew.
7. THE Source_Crew SHALL quiesce the session before serialization such that no turn is in flight during the handoff.
8. WHEN a monitor loop is armed on the session, THEN THE Preflight SHALL report it and THE migration SHALL either carry the loop or explicitly disarm it, and SHALL NOT leave it armed on both crews.
9. WHEN the migration completes, THE Source_Crew's session SHALL be a Tombstone that displays its new home and SHALL NOT accept new turns.
10. THE migration SHALL be exposed as `kirocrew session move <session-id> --to <crew>` and as a "Move to crew…" action on the session.
11. THE Source_Crew SHALL retain the transcript for history; the Tombstone SHALL NOT delete it.

### Requirement 6: Task-Runner Run Migration

**User Story:** As a user with a long autonomous run going, I want to move it to my remote crew so it keeps making progress, and I want to be told plainly if it can only restart rather than resume.

#### Acceptance Criteria

1. THE TaskRun_Unit Migration_Bundle SHALL carry the run's serialized `Project` record, including its task list with per-task status, `current_task`, `WorkingMemory`, `replan_count`, and spec content.
2. THE Preflight SHALL evaluate whether the Target_Crew can resume the run, and SHALL classify the outcome as `resume` or `restart`.
3. WHEN the run's git state — `repo_root`, `worktree_path`, `branch_name`, or `commit_hashes` — cannot be reproduced on the Target_Crew, THEN THE Preflight SHALL classify the migration as `restart` and SHALL name the unreproducible reference.
4. WHEN the classification is `restart`, THEN THE migration SHALL require explicit user confirmation and SHALL state what progress is discarded; THE migration SHALL NOT restart silently.
5. WHEN the classification is `resume`, THEN THE Target_Crew SHALL continue from the run's recorded position and SHALL NOT re-execute tasks already recorded complete.
6. THE Source_Crew SHALL bring the run to a paused, persisted state before serialization, and SHALL NOT serialize a run with a task mid-execution.
7. WHEN a task is awaiting human approval at migration time, THEN the arriving run SHALL still await approval on the Target_Crew and SHALL NOT be auto-approved by the move.
8. IF the migration fails at any point, THEN THE Source_Crew's run SHALL be resumable in place with its progress intact.

### Requirement 7: Observability and Reversibility

**User Story:** As a user who just moved work, I want to see that it arrived and be able to move it back, so a migration is not a one-way door I have to trust blindly.

#### Acceptance Criteria

1. THE migration SHALL report a terminal outcome of `migrated`, `refused`, or `failed`, and `refused` SHALL be distinguishable from `failed`.
2. WHEN a migration succeeds, THE user SHALL be shown the unit's identity on the Target_Crew.
3. THE Tombstone SHALL be discoverable from the surface that listed the unit before the move.
4. A Migration_Unit that has been migrated SHALL be migratable again from its new owner, including back to the original crew.
5. THE migration SHALL emit a duration measurement per unit type, so handoff latency is observable.
