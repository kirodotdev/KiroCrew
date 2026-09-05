## Problem / Motivation

A unit of work — a cron schedule, a chat session, a task-runner run — cannot be
**moved** from one crew to another. The repo has the neighbours, and both are
**copies**: `session_transfer.py`'s own docstring says *"Copy, never move,"* and
federated session search never mutates the peer. Nothing releases ownership.

So today a user who wants work to run on a different crew has to recreate it by
hand on the target and remember to delete or disable the original. There is no
mechanism that guarantees they did, and nothing records where the work went.

## Why it matters

For a unit that **executes**, two owners is not a duplicate record — it is the
job running twice. A cron schedule live on two crews sends two Slack messages and
performs two writes per fire. A task-runner run resumed on both re-executes
side effects that already happened. A hand-migration that half-succeeds produces
exactly that state, silently.

The inverse failure is just as bad: a user disables the source first, the target
setup fails, and the work is now running nowhere while looking as if it moved.

That is why this needs a protocol rather than a copy plus a note in the docs.

## What changed (motivation → approach → change)

**Goal** — move a live unit so that afterwards *exactly one* crew owns it.

**Approach** — a single-owner handoff, with the ownership transfer as the
primitive rather than the serialization (which already existed):

```
preflight → quiesce → transmit → durable ack → tombstone → release
```

bound by one invariant:

> **Any failure short of a durable ack leaves the SOURCE owning the work.**

Not merely "leaves it consistent" — leaves it *executing, here*. The source is
released only after the target has fsync'd its acceptance.

Alternatives considered and rejected:

- **Extend `session_transfer` with a delete-after-copy.** Rejected: the gap
  between copy and delete is precisely the double-execution window, and it has no
  answer for a crash inside that gap.
- **Two-phase commit across crews.** Rejected as disproportionate: 2PC needs a
  coordinator that outlives both parties, which is a much larger change
  (`rfc-durable-run-coordinator` territory). A source-biased handoff plus a
  reconciliation read gets single-ownership without one.
- **Optimistic move with a repair job.** Rejected: repair means detecting
  double-execution *after* the duplicate side effects have landed.

**What was built:**

- `migration/protocol.py` — the coordinator, the data model, allow-list
  serialization, the credential scan, and `reconcile()` for the ack→tombstone
  crash window. `refused` is kept distinct from `failed`: refused means nothing
  was disturbed and a retry will refuse again; failed means it was attempted and
  rolled back. Preflight **refuses rather than falling back** to a default agent —
  a cron job that quietly changes which agent runs it has been altered, not moved.
- `migration/receiver.py` — durable accept (`atomic_write` + fsync), idempotent on
  `handoff_id`, validates `bundle_version` per kind, and **re-scans for credential
  material**, because a target that trusts the sender has no defence against a
  compromised or older source.
- Three unit adapters behind one seam — cron (double-fire guard; `session_key`
  re-bound), session (quiesce = block new turns + drain the in-flight one; ledger
  carried; monitor loops armed on exactly one side), task-run (git-reproducibility
  probe deciding resume vs restart; restart requires explicit confirmation naming
  the discarded progress; quiesce at a task boundary; pending approvals preserved).
- `migration/tombstones.py` — durable, queryable tombstones, so the surface that
  listed the unit before the move can say where it went.
- Three planning CLI verbs and three planning endpoints, plus the dashboard UI.

**Serialization is allow-list, never exclude-list**, with a drift-guard test
asserting the ship/drop sets partition every dataclass field. An exclude-list
ships anything added later by default; an allow-list drops it and the guard test
makes the omission loud. Findings name the **reference key**, never the value, so
a dropped `project` never puts a host-local path into a bundle, a log, or the
audit trail.

### What is NOT wired — read this before reviewing

This PR is deliberately **preflight, planning, and the durable tombstone read
model**. It is not an end-to-end move, and `docs/system-specs/modules/instances.md`
§16.9 says so rather than leaving a reviewer to discover it:

- **Zero production call sites** for `MigrationCoordinator`, `migrate()` or
  `reconcile()`; the coordinator is only re-exported from `migration/__init__.py`.
- **No transmit transport.** `MigrationReceiver` over a real Instances tunnel is
  not implemented; the tested receiver is local.
- **`reconcile()` has no startup caller**, so the crash window does not converge on
  its own. That is *why* nothing calls `migrate()` yet — wiring a handoff whose
  crash window cannot resolve would be worse than not wiring it.
- **Requirement 7.2 (show the target identity on success) is undemonstrable**:
  `MigrationResult` carries the identity, but nothing produces a success to show it
  on. Requirement 7.3 (discoverability) **is** delivered, for all three kinds.
- **`build_requirement_probe()` must be injected in production.** An absent probe
  makes preflight return no findings — silently skipping every requirement check.
- `api_chat_slot_import` → `import_session_core` is not wired; the pure core is
  tested, but adapting an aiohttp handler to it needs a live dashboard.

Both CLI move verbs and all three endpoints **plan only**.
`kirocrew session move` refuses with exit 2 on purpose: a session bundle is only
coherent snapshotted from the live slot with new turns blocked, and the CLI cannot
quiesce a slot it does not own.

## Tests

**184 tests** across `test/test_migration_*.py` and `test/test_api_migration_move.py`.
What they lock in, rather than a count:

| Area | Behaviour locked in |
| --- | --- |
| Protocol | Rollback on every pre-ack failure path; `refused` ≠ `failed`; audit entry per step with outcome + duration; an audit sink that raises does not fail the migration |
| `reconcile()` | Resolves the crash window to exactly one owner; a receiver that cannot answer **raises** rather than reporting "target holds nothing" |
| Receiver | Durable + idempotent on `handoff_id`; unknown bundle kind/version refused; credential re-scan refuses before materialize and names the pattern, never the value (asserted: no `AKIA` in the message) |
| Allow-lists | Drift guards fail if any `CronJob` or `Project` field has no explicit ship/drop decision |
| Cron | `should_fire()` false after tombstone regardless of clock advance (double-fire guard) |
| Session | Non-portable references reported not swallowed; ledger carried; monitor loop armed on exactly one side; requirements derived with `mcp_server` blocking |
| Task-run | resume/restart classification; restart gated on confirmation; completed tasks not re-executed; approvals preserved; in-place resumability on failure |
| Reversibility | Single ownership at **every hop** of a round trip, including back to the original crew |
| Integration | Real adapter + coordinator + receiver over a loopback pair |
| Tombstones | Survives restart; kinds do not collide; corrupt file degrades to "nothing moved"; `clear()` on materialize; only the four redirect fields persisted |
| Endpoints | Full error contract (400/404/409/503); gateway plans from live state with no fidelity gap |
| Frontend | 4 files, 29 tests (dialog validation, menu item, badge incl. accessible name) |

Every one of the 10 commits is green: 39 → 52 → 92 → 137 → 146 → 184.

**Mutation testing, `.kiro/specs/crew-work-migration/tools/mutation_sweep.py`:
23 mutations, 23 caught.** It exists because three groups of tests did *not* get a
clean red→green — the slice-1 protocol tests (the shell was blocked at the time),
the reversibility/integration tests (green on first run by design), and the
frontend wiring (implemented before its tests). Rather than claim TDD that did not
happen, each is subjected to a mutation. Its **first run found two survivors, and
neither was a code bug** — both were toothless *tests*: a reversibility assertion
that could not distinguish a completed handoff from the ack→tombstone crash
window, and an integration test that **mirrored** the adapter's `session_key`
re-bind locally, so it asserted against its own copy of the behaviour.

## Manual verification

- `kirocrew cron move`, `taskrun move` and `session move` exercised against the
  real installed binary: plans printed with handoff id, shipped-field count and
  fidelity findings; `session move` exits 2 with its reasoned refusal.
- `npx tsc --noEmit` clean; the four affected frontend test files re-run green
  after each rebase.
- **Full suite on this PR's base**: 104 failed / 82158 passed / 2 errors. The base
  itself reports **103** failures on this host, measured by running the failing
  files on an `origin/main` worktree and diffing the FAILED sets. They are
  environmental and stable (`KIROCREW_HOME` → real `~/.kiro`, `/home` owned by uid
  65534, a memory-bounded xdist worker budget, an AF_UNIX path-too-long,
  `file_explorer`/`issue_radar`/`source_provider` sandbox permissions). The single
  difference is `test_memory_smoke.py::…::test_custom_agent_gets_hook_transform`,
  which passes standalone on both refs (3 consecutive runs, 49/49 for its file) and
  appeared in one of three full runs — an order/parallelism flake. **No failure
  lands in any file this PR touches.**
- Two earlier full runs, against the previous base, **exposed two real regressions
  that every subset run had missed**, both fixed at the root here: `session` and
  `taskrun` were registered with a raw `sub.add_parser()`, bypassing
  `cli_help.add_command` — the guard that raises `KeyError` for an ungrouped
  command and so makes help drift impossible (fixed by grouping **and** routing
  through the helper; grouping alone leaves the next command free to drift); and
  `_SECRET_PATTERNS` hand-spelled the AWS key-ID prefix group, a sixth copy, now
  importing `credential_patterns.AWS_KEY_ID`. Neither failing test lives near a
  migration file, which is exactly why a green subset was not sufficient.
- **Still required, and I cannot do it here:** the dashboard paths need a live
  gateway — the "Move to crew…" dialog on the Schedule / Projects / session
  surfaces, and the migrated-to badge rendering on a genuinely tombstoned job. The
  registry write, the API field and the component are unit-tested, but the wired
  visual result is not something I can verify in this environment.

## Screenshots / video

**Not captured — and this section is kept rather than deleted, because the PR does
change user-visible UI** (a "Move to crew…" action on three surfaces and a
migrated-to badge in the Schedule status cell).

Rendering them honestly needs a live gateway with a genuinely tombstoned cron job,
which this environment does not have; a mocked screenshot would assert nothing the
component tests do not already assert. Happy to add them if a maintainer prefers
that before review, or to accept this as a blocker.

## Related Issues

Refs #7577 — **this PR does not close it.** It lands the protocol, the three
adapters, the planning surfaces, tombstone discoverability and the audit trail;
the end-to-end move needs the transmit transport listed above.

Governance: RFC at `docs/request-for-change/rfc-crew-work-migration.md`, indexed in
that directory's README, declaring a **non-blocking** relationship to
`rfc-durable-run-coordinator` while adopting its vocabulary. Spec chain in
`.kiro/specs/crew-work-migration/`, where `REVIEW.md` is an evidence dossier with
an **unsigned verdict** — an agent produced these artifacts and must not approve
them.

One RFC claim was corrected in the same PR that revealed it: the motivation said
the task-runner's `Project` is "persisted to `runs.json`", implying full fidelity.
`runs.json` is a **subset** — no `WorkingMemory`, no `current_task`. Fixed per that
directory's rule that code wins and the document is the bug.

Not related, stated because they are adjacent and might be assumed: #7822 and
#7522 (remote-crew bootstrap resolving `desktop-release.q.us-east-1.amazonaws.com`
inside the Amazon Q VPC endpoint's private-DNS namespace) are a different
subsystem. This PR touches no `cloud/ec2.py`, no CloudFormation template and no
bootstrap code, and shares zero files with them.

## Checklist

- [x] At most two commits (one is the norm), with a Conventional Commits title
- [x] Existing tests pass and new tests added for new functionality
- [x] Self-review completed; code follows project style guidelines
- [x] Documentation updated (`docs/system-specs/modules/instances.md` §16,
      `docs/system-specs/modules/cli.md`, and the RFC)
- [x] No secrets, credentials, or internal references in the diff
