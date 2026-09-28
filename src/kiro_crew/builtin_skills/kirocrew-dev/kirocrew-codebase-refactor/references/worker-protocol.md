# Worker and Orchestrator Protocol

This reference belongs to `kirocrew-codebase-refactor` and applies only to Kiro
Crew refactor workers. Every implementation writer must also follow
`kirocrew-worktree-dev` and the owning repository specs.

Read this reference before delegating analysis or implementation. A worker is not an autonomous owner of campaign scope; the coordinator owns decomposition, path claims, integration, and completion claims.

## Coordinator responsibilities

Before dispatching a writer:

1. Refresh upstream and freeze the worker's base SHA.
2. Confirm that the target is not already implemented or actively owned elsewhere.
3. Create or verify an isolated checkout when available.
4. Assign exclusive owned paths and explicit forbidden/shared paths.
5. Provide behavior invariants, validation commands, delivery authority, and stop conditions.
6. Tell the worker what every other active writer owns.
7. Give the task a stable, trackable title.

The coordinator must continue useful local work while workers run when possible. Do not spawn overlapping reviewers or writers merely to fill capacity.

## Required worker brief

Every implementation brief includes:

```text
Task title:
Repository and base SHA:
Mode and requested endpoint:
Problem/evidence:
Desired ownership boundary:
Owned paths:
Adjacent read-only paths:
Forbidden/shared paths:
Behavior/API invariants:
Repository instructions to read:
Baseline commands:
Required target gates:
Expected structural outcome:
Other active scopes:
Commit/push/PR authority:
Delegation authority:
Stop/redesign conditions:
Required final report:
```

Do not tell a worker only “simplify this file.” Ambiguous goals encourage line-count optimization, behavior changes, and scope expansion.

## Worker execution contract

The worker must:

- verify repository identity, branch, HEAD, and cleanliness before editing;
- read applicable repository instructions itself;
- report any pre-existing dirty state instead of overwriting it;
- map callers, callees, public surfaces, side effects, and relevant tests;
- run or record a baseline gate before changes when feasible;
- stay inside owned paths, requesting coordinator action for a shared file;
- keep behavior changes separate and stop if one appears necessary;
- checkpoint after meaningful verified steps;
- report exact commands, exit codes, failures, and skipped/unavailable gates;
- refresh upstream overlap before delivery;
- never claim PR/CI/merge state from memory;
- never force-push, merge, close, delete, or clean up without authority.

Recursive delegation is disabled unless the brief explicitly permits it. If enabled, the child must receive the same ownership and reporting discipline, and the original worker remains accountable for synthesis.

## Verified checkpoints

Long or expensive scopes should produce small recoverable checkpoints. If commits are authorized, one coherent verified step per commit makes interruption recovery and blame easier; project history policy may later squash them. If commits are not authorized, keep a checkpoint manifest plus a cleanly inspectable diff.

A checkpoint records:

- phase and current HEAD;
- files changed since the previous checkpoint;
- behavior boundary completed;
- verification evidence;
- remaining work and known risks;
- any new upstream drift;
- background processes that still matter and who owns them.

Never leave a background test or service ownerless. If the harness cannot hand it to the coordinator, stop it or record that its result will not be delivered.

## Status vocabulary

Workers report one of:

- `SCANNING`
- `DESIGN_READY`
- `IMPLEMENTING`
- `TARGET_GREEN`
- `INTEGRATION_GREEN`
- `REVIEW_READY`
- `EVIDENCE_LIMITED`
- `NEEDS_COORDINATOR`
- `PARKED`
- `SUPERSEDED`
- `FAILED_WITH_EVIDENCE`

`EVIDENCE_LIMITED` means the audit/plan is complete for the supplied static inputs but executable or upstream proof is unavailable. Avoid vague messages such as “almost done” or “looks green.” A status update names the evidence and next action.

## Coordinator collection and verification

When a worker returns:

1. Inspect Git status, commits, and diff independently.
2. Verify changed files are inside the path claim.
3. Review the structural result, not only test output.
4. Rerun the critical target gate or validate the worker's fresh, complete evidence.
5. Compare against latest upstream and active changes.
6. Decide whether to integrate, request a bounded correction, rebuild a residual change, park, or discard with user authority.

Worker self-report is a lead, not proof. A worker may be interrupted while its commit remains valid; conversely, a completed session may have no published or mergeable change.

## Parallel review pattern

Read-only review may use independent lenses without writer conflicts:

- behavior/API compatibility;
- code reuse and duplicate knowledge;
- clarity/cohesion/comments;
- efficiency/resource behavior;
- tests and mutation strength;
- packaging/platform/security.

The coordinator deduplicates findings, resolves conflicting optimization goals, and sends one prioritized correction list to the writer. Multiple reviewers must not edit the same checkout.

## Drain policy

Before a new wave, classify every current scope:

- merged/delivered;
- active and fresh;
- local-only but salvageable;
- stale and requires residual rebuild;
- superseded;
- explicitly abandoned.

Do not hide orphaned work by starting more agents. Drain means integrating, rebuilding, parking with a precise handoff, or abandoning with recorded evidence.
