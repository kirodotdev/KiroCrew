---
title: Leaked Runtime Reclaim — one reconciler, install-scoped ownership proof
status: draft
kind: change
author: iamwhatever
created: 2026-10-01
last-audited: 2026-10-01
audited-at: 2db531ec89
doc-pr: 15748
implementation-prs: [14510]
tracking-issues: [11899, 11789, 8133, 13324, 11991]
supersedes: []
superseded-by: []
---

# RFC: Leaked Runtime Reclaim — one reconciler, install-scoped ownership proof

- Status: draft. Acceptance requested from a maintainer; the status flips to `accepted` when one records it here. Step A of the rollout needs no record because it adds no default kill authority. Steps B and C extend what the gateway kills by default, and the First Principles lane reads that decision from the base branch, so this document lands on its own first.
- Author: iamwhatever
- Created: 2026-10-01
- Related: `src/kiro_crew/runtime_reconcile.py`, `src/kiro_crew/runtime_ownership.py` (`authorize_runtime_kill`), `src/kiro_crew/session_pid.py` (`_is_untracked_managed_agent_orphan`), `src/kiro_crew/session_scope_reap.py` (`instance_slice_pids`)

## Summary

An agent runtime that outlives its session and is absent from every record is a leak no sweep ends. On a busy host these leaks pile up until the host runs out of memory and the gateway restarts. This RFC settles who may kill such a process, on what proof, and in what order the authority rolls out.

The answer is one reclaim arm: the runtime reconciler already on main. It gains a second source of candidates (non-cgroup Linux hosts) and a second kind of proof (a persisted, install-scoped spawn record), and nothing else gets a kill path. The user can see the leak on the dashboard and reclaim it once, by hand, through the same gates.

## Motivation

### Current state (measured at `2db531ec89`)

Two arms look at the same population and disagree about what to do with it.

| Arm | Where | Sees | Acts |
|---|---|---|---|
| Reconciler | `runtime_reconcile.py` | pids in this install's agent cgroup slice (`instance_slice_pids`) | kills, default budget `DEFAULT_MAX_KILLS = 5` per pass |
| Untracked report | `session_pid.py`, `_is_untracked_managed_agent_orphan` | init-reparented runtimes with `KIROCREW_SPAWNED` and no PID-file row | logs only; its docstring reserves the kill for a maintainer ruling |

The reconciler requires five conditions before a signal: this install's spawn marker, unowned on the previous pass too, older than `DEFAULT_MIN_AGE_SECS` (300 s), an argv naming a managed harness, and `authorize_runtime_kill` allowing it. `session.reconcile_max_kills` can only lower the budget; at `0` it audits each would-be kill as `would_kill` and signals nothing.

Each ACP runtime spawn mints a `KIROCREW_SPAWN_INSTANCE` token (`acp/runtime.py`, held as `_process_instance`) and stamps it on the child's environment. For runtimes that token lives only in gateway memory. App backends already persist the same token beside pid and start time in their pid file (`apps/backend_runtime/pidfile.py`, `_record_app_pid`), which is the precedent this RFC follows.

### Problems

1. Non-cgroup Linux hosts have no reclaim. `instance_slice_pids` returns the empty set when the slice cannot be resolved, and the reconciler then compares nothing. The report arm sees these leaks and is not allowed to act.
2. Proof dies with the gateway. Leaks cause OOM, OOM restarts the gateway, and after a restart no in-memory token can vouch for anything the previous process spawned.
3. Two arms with two vocabularies. A fix that adds a kill to the report arm (the closed #12612) builds a second, parallel kill path beside the reconciler, and every review round found another live shape that passed its checks.
4. The user cannot see or end a leak. The reading is a log line and a `resource_status` field.

## Goals

- One kill path for unowned runtimes, with one set of refusals.
- Ownership proof that survives a gateway restart and cannot authorize a kill on its own.
- Default reclaim on Linux hosts with or without cgroup delegation.
- A visible leak count and a user-confirmed reclaim on the dashboard.

## Non-goals

- Tracked teardown, scope reaping and app-backend stale reap. Those paths already end what their records name and stay as they are.
- macOS and Windows default reclaim (see Decision 5).
- Reclaim across installs. A process another data home spawned is never ours.
- Raising the per-pass budget above `DEFAULT_MAX_KILLS`.

## Decisions

### 1. One reclaim arm: the reconciler

The reconciler in `runtime_reconcile.py` is the only code that kills an unowned runtime. The report arm in `session_pid.py` stops being a separate verdict: its hits become a candidate source the reconciler reads, and its output becomes part of the reconciler's `would_kill` reading. It gains no signal of its own.

Reason: the reconciler already carries the two-pass confirmation, the age floor, the managed-argv check, the lease gate, the budget and the observe switch. A second arm would have to re-derive each of them and would drift from them. The closed #12612 was that second arm, and it conflicted with main twice in the week the reconciler landed.

### 2. Kill authority: default-on, budgeted, with an observe switch

Reclaim stays default-on with the per-pass budget `DEFAULT_MAX_KILLS`. `session.reconcile_max_kills=0` remains the observe switch and keeps its meaning on every new candidate source: evaluate every gate, audit `would_kill`, signal nothing.

Reason: a leak that nothing ends grows until the host fails, so a default of off leaves the reported failure in place. The budget makes a reconciler that is wrong about a population wrong slowly, and the switch gives a shared-data-home host the reading without the signal. Main already ships this default on cgroup hosts; this RFC records it as the decision and extends it to the hosts in Decision 5.

### 3. Ownership proof is install-scoped and subtractive

A runtime counts as "ours" only through a persisted spawn record written per data home. Each row holds the minted `KIROCREW_SPAWN_INSTANCE` token, the root pid and the root's spawn start time. A candidate qualifies when its environ token matches a row and its current start time equals the row's.

The record is subtractive only. A missing row, a token mismatch, a start-time mismatch or an unreadable record means no kill. A matching row can veto nothing it does not already veto and authorizes nothing by itself: the candidate must still pass every gate in Decisions 1 and 4.

Reason: install scope is what survives the restart that leaks cause (Problem 2), and the data home is already the install boundary the agent slice is keyed on. The token proves the tree is ours; the start time proves the pid is still the same process. The record lives in a same-uid-writable file, so it must never be able to grant a kill an agent could not cause anyway; keeping it subtractive caps the damage of a forged or stale row at a missed reclaim.

### 4. Refusals that always win

The reconciler refuses a candidate when any of these hold:

| Refusal | Proof read |
|---|---|
| Leased or under tenancy | `authorize_runtime_kill` returns false |
| Session leader alive | the candidate's sid names a live process other than the candidate |
| Group leader alive | the candidate's pgid names a live process other than the candidate |
| Tracked or protected | the pid is in a PID file or `register_protected_pid` |

A sid or ppid value never grants a kill. Both are read only to refuse. On main the reconciler applies the lease gate and the tracked check; the two leader refusals are new and apply to every candidate source, so the slice source and the non-cgroup source answer to one list.

Reason: every real bug the lanes found in #12612 had one shape. The env stamp is inherited, so "carries our stamp" is true of a live runtime's descendants too, including a detached `setsid` child and a member of a live runtime's group. These four refusals cover each live shape seen so far, and none of them can be satisfied by a value the agent can choose.

### 5. Platforms

| Platform | Candidate source | Default |
|---|---|---|
| Linux, cgroup delegated | agent slice (`instance_slice_pids`) | reclaim (on main) |
| Linux, no cgroup | init-reparented runtimes from the report arm, proven by Decision 3 | reclaim, from step C |
| macOS | report arm reading | report only |
| Windows | report arm reading | report only |

macOS and Windows stay report-only until each has a native proof of ownership: on Windows, job objects; on macOS, a proof that does not rest on `KERN_PROCARGS2` environ reads in a fast-wrapping pid space.

Reason: the Linux proof uses `/proc/<pid>/environ`, sid and pgid. Neither other platform offers the same reads at the same cost, and a port that guesses would trade a missed reclaim for a wrong kill.

### 6. User control on the dashboard

The dashboard shows the leaked count and its total RSS, read from the reconciler's reading. A "Reclaim" button, after the user confirms, runs one reconciler pass over the current candidates with the same gates. It does not raise the budget above `DEFAULT_MAX_KILLS`, does not skip a refusal and does not run when the observe switch is `0` unless the user confirms that pass explicitly.

Reason: the user is the one person who knows no other install shares the data home. A confirmed pass gives them a way to end a leak on a host where default reclaim is off or not yet reached, without adding a path that bypasses the gates.

### 7. Rollout

| Step | Ships | New default authority | Exit criterion |
|---|---|---|---|
| A | report arm hits feed the reconciler reading; dashboard count + RSS; confirmed Reclaim button | none | a leaked runtime on a non-cgroup host shows on the dashboard, and a confirmed Reclaim ends it through `authorize_runtime_kill` |
| B | persisted per-data-home spawn record (Decision 3) for ACP runtimes | none | after a gateway restart the reconciler reading still names the previous process's leaked runtimes as ours, and a row with a mismatched start time yields no kill |
| C | default reclaim on non-cgroup Linux using the step B record | yes, under this RFC | a non-cgroup host's leaked runtimes are reclaimed within the budget with no manual pass, and `reconcile_max_kills=0` turns it into `would_kill` only |

Each step is its own PR and can be abandoned without the next. Step A is in flight as a separate PR.

## Backward compatibility

`session.reconcile_max_kills` keeps its default (`5`), ceiling and meaning. A data home with no spawn record reads as "no proof" and reclaims nothing new, so step B is inert on its first start and older rows simply never match. The report arm's log line stays until step A's reading replaces it.

## Security considerations

| Threat | Mitigation |
|---|---|
| Env-stamp inheritance onto live descendants | stamp only narrows; session-leader, group-leader, lease and tracked refusals decide liveness (Decision 4) |
| PID reuse | start time recorded at spawn and rechecked before each signal; mismatch retracts the row and never signals |
| Same-uid-writable record | record is subtractive (Decision 3); a forged row can only mark a process the agent could already signal, and still faces every refusal |
| Shared data home across installs | observe switch `0`; dashboard Reclaim needs user confirmation |
| Population misjudged | per-pass budget `DEFAULT_MAX_KILLS`, two-pass confirmation, 300 s age floor |

## Alternatives considered

- Add the kill to the report arm in `session_pid.py`. Rejected: a second kill path with its own gates, the shape of the closed #12612 (Decision 1).
- Process-scoped proof (in-memory token set). Rejected: lost on the restart that leaks cause (Decision 3).
- Default off everywhere until a user opts in. Rejected: the reported failure is a host that runs out of memory unattended (Decision 2).
- Trust sid or ppid as ownership. Rejected: the sid of a measured leak names a dead launcher, and both values are inherited or reparented (Decision 4).

## Resolves

| Issue | How |
|---|---|
| [#11899](https://github.com/kirodotdev/KiroCrew/issues/11899) | detected leaks are reclaimed (steps A–C) |
| [#11789](https://github.com/kirodotdev/KiroCrew/issues/11789) | closed sessions' runtime trees are reclaimed by the reconciler's tree signal |
| [#8133](https://github.com/kirodotdev/KiroCrew/issues/8133) | unowned slice processes are counted and reclaimed |
| [#13324](https://github.com/kirodotdev/KiroCrew/issues/13324) | leaked MCP members of a runtime group are reclaimed with their root |
| [#11991](https://github.com/kirodotdev/KiroCrew/issues/11991) | Windows stays report-only and gains the dashboard reading; reclaim waits on a job-object proof |

## Open questions

1. Where the spawn record lives: a new file under the data home or new columns on the existing PID file. Step B decides; either way the record is subtractive.
2. Whether the dashboard Reclaim may run when the observe switch is `0`. This RFC allows it only on an explicit confirmation of that pass; a maintainer may prefer to hide the button instead.
