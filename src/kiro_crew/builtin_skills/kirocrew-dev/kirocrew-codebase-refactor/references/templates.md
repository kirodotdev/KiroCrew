# Kiro Crew Refactor Templates

These templates belong to `kirocrew-codebase-refactor` and apply only to the
Kiro Crew source repository or one of its worktrees. Replace every placeholder
with current repository evidence before sending a brief.

## Campaign scan or reset

```text
Use kirocrew-codebase-refactor in PLAN or AUDIT mode.

Kiro Crew checkout: <absolute path>
Upstream: origin/main at <exact SHA and timestamp>
Goal: <deduplicate / simplify / remove stale comments / decompose modules/classes>
Behavior policy: preserve observable behavior unless an explicit exception is listed.
Delivery endpoint: plan only; make no code or remote changes.

Read root AGENTS.md and applicable owning specs. Measure first-party runtime under
src/kiro_crew and website/src with explicit exclusions. Inventory large files,
classes and functions; duplicated policy; cycles; churn; owner tests; active
branches and PRs; and agent-navigation cost where practical. Rank cohesive scopes
by payoff, test strength, churn, overlap, coupling, platform risk and review cost.
Return disjoint waves, one integration owner for shared files, target gates,
structural acceptance criteria, counter-metrics and a drain plan for existing work.
```

## Implementation worker brief

```text
Task title: <stable title>
Kiro Crew worktree: <absolute path>
Base: origin/main at <exact SHA>
Mode: EXECUTE
Requested endpoint: <diff-ready / local commit / PR>

Problem and evidence:
<current size, complexity, duplication or ownership problem>

Desired ownership boundary:
<which responsibility becomes canonical where>

Owned paths:
- <path>

Adjacent read-only paths:
- <path>

Forbidden/shared paths and integration owner:
- <path -> owner>

Other active scopes:
- <scope -> owned paths>

Behavior and Kiro Crew invariants:
- <imports/signatures/errors/side effects/order/schemas/config/platform behavior>
- Read root AGENTS.md and every owning spec before editing.
- Preserve OSS-fork, security, governance, harness-parity and cross-platform rules.

Required evidence:
- baseline: <commands/artifacts>
- target: <commands>
- compatibility/parity: <commands/artifacts>
- package/platform: <commands>
- current upstream and open-PR overlap before editing and before delivery

Structural acceptance:
- <canonical owner, hotspot threshold, no new cycle, etc.>

Authority:
- edit: <yes/no>
- commit: <yes/no>
- push/PR/comment/merge: <specific subset>
- recursive delegation: <yes/no>

Stop and report if upstream has implemented the intent, drift crosses forbidden
paths, equivalence cannot be demonstrated, a behavior change is needed, a gate
would need weakening, or cycles/indirection increase without payoff.

Final report: HEAD/base, changed files, before/after structure, exact commands and
exit codes, compatibility evidence, upstream drift, counter-movers, remaining
risk and next action. Do not equate local completion with delivery.
```

## Stale or local-only recovery

```text
Use kirocrew-codebase-refactor in RECOVER mode. Do not modify the old branch until
the audit is complete.

Inspect the local commit/worktree, remote branch and PR, and current origin/main.
Prove whether the intended extracted modules and behavior already exist upstream.
Compute ahead/behind, owned-path changes, active intent overlap and semantic risk.

Classify the work as clean replay, bounded manual integration, fresh-base residual
rebuild, superseded, or unrecoverable. If implementation is authorized, preserve
the compatible residual only and run target plus integration gates. Load
prepare-pr for every publication step. Never force a stale diff through broad
conflicts merely to preserve the original branch.
```

## Independent compatibility reviewer

```text
Perform a read-only review of <branch/diff> against <base SHA>. Do not edit files
and do not rely on the implementer's conclusion.

Check missing or identity-changed public/plugin symbols; signature/default/error/
ordering drift; dynamic imports and monkeypatch seams; serialization, schemas,
config, CLI, prompt, wire or i18n changes; async/locking/process/platform behavior;
packaging omissions; weakened tests or baselines; duplicate implementations; new
cycles; forwarding sprawl; and behavior changes hidden in comment/dead-code cleanup.

Return evidence-backed findings with path/symbol, baseline behavior, branch
behavior, severity and a verification method. State what could not be inspected.
```

## Campaign status

```markdown
## Outcome
<continue / drain / redesign / pause / complete, with one-sentence evidence>

## Baselines
- Frozen: `<sha>` at `<time>`
- Latest upstream: `<sha>` at `<time>`
- Runtime scope and exclusions: ...

## Structural change
| Metric | Frozen | Landed | Latest upstream | Delta |
|---|---:|---:|---:|---:|
| Runtime LOC | | | | |
| Files >= threshold | | | | |
| Large functions/classes | | | | |
| Modules/import edges | | | | |
| Agent lookup tokens | | | | |

## Delivery funnel
| Scope | Structural outcome | Local state | Remote state | Drift/overlap | Next action |

## True progress
- Touched coverage: ...
- Baseline hotspots eliminated: ...
- Tooling/support work: ...
- Residual hotspots in touched scopes: ...

## Evidence and risks
- Required gates: ...
- Compatibility/package/platform: ...
- Inherited/flaky/advisory failures: ...
- Counter-movers: ...

## Recommendation
<next drain/wave, concurrency rationale and forecast assumptions>
```
