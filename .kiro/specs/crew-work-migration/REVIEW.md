# REVIEW — crew-work-migration (#7577)

> **Separation of duties.** Every artifact below was produced by the agent. This
> file is the evidence dossier a reviewer needs, **not an approval**. The verdict
> section is deliberately unsigned: the product owner adjudicates.

- **Branch:** `feat/crew-work-migration-slice1` (41 commits ahead of `main`, never pushed)
- **Spec:** `.kiro/specs/crew-work-migration/`
- **RFC:** `docs/request-for-change/rfc-crew-work-migration.md` (indexed in that directory's README)

---

## 1. What this change is

Move a live unit of work — a cron schedule, a chat session, or a task-runner run
— from one crew to another. The genuine missing piece was never the payload; it
is the **single-owner handoff protocol**:

```
preflight → quiesce → transmit → durable ack → tombstone → release
```

with one binding invariant:

> **Any failure short of a durable ack leaves the SOURCE owning the work.**

Not "leaves it consistent" — leaves it *executing, here*. For a unit that
executes, two owners is not a duplicate record, it is the job running twice.

The existing neighbours are both copies: `session_transfer.py`'s docstring says
"Copy, never move," and federated search never mutates a peer. Nothing in the
codebase moved ownership before this.

## 2. Scope boundary

Adjudicated by the product owner on 2026-09-01:

- **Sequenced** cron → session → taskrun, not parallel.
- **Standalone** feature, not folded into #4923.
- **Project rematerialization out of scope** — a missing checkout is reported as
  a `HostRequirement`, never recreated.
- **#7522 deliberately excluded.** Thematically adjacent (remote crew VPC/DNS
  packaging) but a different defect.

## 3. What a reviewer should look at first

| Concern | Where | Why it matters |
| --- | --- | --- |
| The invariant | `migration/protocol.py` `MigrationCoordinator.migrate` | Rollback on every pre-ack failure; `refused` vs `failed` |
| Crash-window convergence | same file, `reconcile` | The only thing that resolves ack→tombstone to one owner |
| What travels | `*_adapter.py` `*_SHIP_FIELDS` / `*_DROP_FIELDS` | Allow-list + drift-guard tests |
| Trust at the boundary | `migration/receiver.py` `accept` | Version validation + receiver-side secret re-scan |
| Discoverability | `migration/tombstones.py` | Durable, queryable, degrades safely |

## 4. Verification

| Gate | Result |
| --- | --- |
| Migration + endpoint suites | **184 passed** |
| Frontend (full suite, run inadvertently and kept) | **1737 files / 27308 passed**, 0 failed |
| `npx tsc --noEmit` | clean |
| Mutation sweep (`tools/mutation_sweep.py`) | **23/23 caught** |
| Tests reading `system-specs` docs | 1992 passed (no drift check broken) |
| Existing cron + CLI suites | 11 failures, **proven pre-existing** by stashing the change and diffing the failure sets — identical |

### 4.1 Why the mutation sweep exists

Three groups of tests did **not** get a clean red→green:

1. The slice-1 protocol tests (12) went straight to green — the shell was blocked
   at the time, so red was never observed.
2. The reversibility and integration tests were green on first run *by design*
   (characterisation of behaviour already built).
3. The frontend menu items and page wiring were implemented before their tests.

Rather than claim TDD that did not happen, each was subjected to a mutation: break
the behaviour, assert a named test fails. The first run found **2 survivors**,
both of which were real weaknesses in the tests, not in the code (see §5).

## 5. Bugs this work found — and who found them

| # | Bug | Found by |
| --- | --- | --- |
| 1 | `accept()` required `payload["unit_id"]`, but an adapter's allow-list legitimately drops the source-local id. Identity belongs to the **bundle**. | An integration test with real components — mocked unit tests had hidden it |
| 2 | A fixture invented `"tasks"`; the real persisted key is **`task_details`**, and `runs.json` carries no `memory`/`current_task` | Reading `taskrunner._serialize_runs`. **No test caught this** |
| 3 | Removing the coordinator's `tombstone()` call left the reversibility test green — the fake's `quiesce()` had already cleared `executable`, so the assertion could not tell a finished handoff from the ack→tombstone crash window | Mutation sweep |
| 4 | Removing the cron adapter's `session_key` re-bind left the integration test green because the test **mirrored** the adapter's logic locally | Mutation sweep |
| 5 | A `logger.debug` in a guard branch of `cli_commands.py`, which has no logging at all — a latent `NameError` that stayed green because the registry swallows its own errors and the branch was never reached | Checking the claim, not a test |

Items 3 and 4 are the reason §4.1 is in this file: a passing test that cannot
distinguish correct from broken is worse than a missing one, because it is
counted as coverage.

## 6. Honest limits — what does NOT work yet

Verified on disk with escaped greps, not recalled:

- **Zero production call sites** for `MigrationCoordinator`, `migrate()`, or
  `reconcile()`. The class is only re-exported in `migration/__init__.py`.
- **No transmit transport.** `MigrationReceiver` over a real Instances tunnel is
  not implemented; the tested receiver is local.
- **`reconcile()` has no startup caller**, so the crash window does not converge
  on its own. That is *why* nothing calls `migrate()` yet — wiring a handoff whose
  crash window cannot resolve would be worse than not wiring it.
- **Req 7.2 is therefore undemonstrable.** `MigrationResult.remote_unit_id`
  already carries the target identity, but nothing produces a success to show it
  on. Req 7.3 (discoverability) **is** done, for all three kinds.
- **`build_requirement_probe()` must be injected in production.** An absent probe
  makes preflight return no findings — silently skipping every requirement check.
  A quiet failure mode, called out in `instances.md` §16.9.
- **`api_chat_slot_import` → `import_session_core` not wired.** The pure core
  exists and is tested; adapting the aiohttp handler to it needs a live dashboard.

Everything shipped is **preflight, planning, and the durable tombstone read
model**. Both CLI move verbs and all three endpoints **plan only**.
`kirocrew session move` refuses with exit 2 on purpose: a session bundle is only
coherent snapshotted from the live slot with turns blocked, and the CLI cannot
quiesce a slot it does not own.

## 7. Risk assessment

| Risk | Severity | Mitigation |
| --- | --- | --- |
| Double execution after a move | **Critical** | `should_fire()` guard; monitor loops armed on exactly one side; `enabled=false` persisted |
| Silent state loss on a new field | High | Allow-list + drift-guard tests fail on any undecided field |
| Credential leaking to another host | High | Scan on source **and** on target; findings name the pattern, never the value |
| Half-understood bundle | High | `SUPPORTED_VERSIONS` refuses unknown kind/version |
| Unit lost in the crash window | High | `reconcile()` — **but it has no caller yet (§6)** |
| Stale tombstone after a move back | Medium | `clear()` on materialize, all three kinds |
| Broken registry hides the schedule | Medium | Reads degrade to "nothing moved" at CLI, endpoint and component |

## 8. SDLC gate state

`sdlc_gate.py` looks for `spec.md` and `plan.md`; this Kiro spec dir uses
`requirements.md`, `design.md` and `tasks.md`. Renaming the Kiro files would break
the tooling that consumes them, and copying their content into gate-named files
would create two sources of truth. So `spec.md` and `plan.md` exist here as **thin
linkage files** carrying only a status — the pattern the skill's own brownfield
guidance calls for.

Both are left at `**Status:** draft` **on purpose**. The agent wrote the artifacts
they point at, so it may not sign them:

| Stage | Gate | Blocked on |
| --- | --- | --- |
| design | **open** | — (`intent.md` is `accepted`) |
| build | closed | `spec.md` → `signed-off` |
| test | closed | `spec.md`, `plan.md` |
| deploy | closed | `plan.md` → `accepted` |

Two one-line edits open every remaining gate. Verify with:

```bash
python3 ~/.kiro/crew/skills/ai-native-sdlc/scripts/sdlc_gate.py \
  .kiro/specs/crew-work-migration deploy
```

## 9. Reviewer checklist

- [ ] The single-owner invariant is genuinely upheld on every failure path
- [ ] `refused` vs `failed` is the right distinction, and drawn in the right places
- [ ] The allow-list ship/drop decisions are correct per field (not merely total)
- [ ] `reconcile()`'s contract is sound — is raising on an unanswerable receiver right?
- [ ] The tombstone file location (`<config dir>/migration/`) is acceptable
- [ ] §6's limits are acceptable to merge behind, or a blocker
- [ ] `bands.yaml` watches the right things (ownership integrity first, latency last)
- [ ] 41 commits: squash, or keep the circle-by-circle history?

## 10. Verdict

_To be completed by the reviewer. The agent does not sign this._

- **Decision:** ☐ approve ☐ approve with changes ☐ request changes ☐ reject
- **Reviewer:**
- **Date:**
- **Notes:**
