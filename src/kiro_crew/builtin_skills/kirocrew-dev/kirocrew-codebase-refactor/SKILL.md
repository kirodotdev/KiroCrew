---
name: kirocrew-codebase-refactor
description: "Kiro Crew maintainers only: plan, execute, recover and measure behavior-preserving refactors of the Kiro Crew source repo. Use for dedup, simplification, stale-comment cleanup, splitting large modules, refactor waves and progress audits. Not for users' projects or one small cleanup."
triggers: kirocrew refactor, refactor Kiro Crew, Kiro Crew codebase refactor, simplify kirocrew, kirocrew deduplication, kirocrew large class, kirocrew large module, kirocrew refactor campaign
repo_scope: src/kiro_crew
---

# Kiro Crew codebase refactor

> **Scope guard:** this skill applies ONLY to the Kiro Crew source repository or
> one of its worktrees. Ignore it for user projects and other repositories. It
> organizes repository-scale structural refactors; it does not replace the
> owning specs, test rules, worktree rules, or PR delivery workflow.

Reduce Kiro Crew's structural complexity without silently changing behavior.
Treat local implementation, publication, review, and merge as different states.

## Choose the operating mode

Infer the narrowest mode supported by the request:

- **PLAN**: scan, rank scopes, estimate risk, and produce staged waves. Do not edit.
- **AUDIT**: inspect structure, branches, worktrees, PRs, CI, upstream drift, and
  true campaign completion. Do not mutate state.
- **EXECUTE**: implement one or more coherent, behavior-preserving refactor scopes.
- **RECOVER**: rescue local-only, stale, conflicted, interrupted, or partially
  superseded Kiro Crew refactor work.
- **INTEGRATE**: reconcile a completed scope with current upstream, then hand
  publication and PR-to-green work to `prepare-pr` when authorized.

Planning or audit authority does not authorize edits. Edit authority does not
authorize commits, pushes, PR mutations, force-pushes, merges, or cleanup.

## Load the sibling that owns the next step

Do not restate or approximate these contracts:

| Work | Required sibling |
|---|---|
| Create a Kiro Crew implementation checkout, build, or run the repository gate | [kirocrew-worktree-dev](../kirocrew-worktree-dev/SKILL.md) |
| Add, edit, diagnose, or speed up tests | [writing-tests](../writing-tests/SKILL.md) |
| Commit, sync, squash, publish, review, or drive a PR to green | [prepare-pr](../prepare-pr/SKILL.md) |
| Monitor an already-published PR when the requested objective fits its provider contract | [babysit](../babysit/SKILL.md) |

Before touching a subsystem, read root `AGENTS.md` and every owning spec it
routes to. Update an owning spec in the same commit when the refactor changes
the ownership or structure it documents.

## Preserve Kiro Crew's invariants

1. **Behavior is the default contract.** Preserve public imports, MCP and HTTP
   schemas, CLI/config behavior, serialized forms, prompt and localized text,
   exception types, side-effect order, initialization order, patch seams,
   platform branches, and concurrency semantics unless a behavior change is
   explicitly approved.
2. **Repository rules outrank generic refactor advice.** The owning spec,
   `AGENTS.md`, `AUTOSDE.yaml`, CI, and existing Kiro Crew conventions decide the
   contract.
3. **Keep the public fork clean.** Never reintroduce internal-only content or
   weaken the OSS boundary, security, governance, harness-parity, or
   cross-platform rules to simplify a diff.
4. **Scope follows ownership, not line quotas.** A useful scope may move several
   thousand lines. Do not split it into microtasks or thin forwarding files just
   to manufacture parallelism or smaller diffs.
5. **Moves and behavior changes stay separable.** Extract first. Isolate and
   explicitly test any unavoidable behavior change.
6. **One canonical implementation.** Compatibility facades may keep old imports,
   entry points, identity, or monkeypatch seams, but two live implementations of
   one rule are not a completed refactor.
7. **Comments explain constraints.** Remove comments that narrate visible code.
   Keep security, compatibility, protocol, concurrency, units, platform, legal,
   suppression, and non-obvious performance rationale.
8. **Deduplicate knowledge, not syntax.** Defaults, errors, retries, permissions,
   logging, ordering, and edge cases must be equivalent before paths are merged.
9. **Baselines shrink only with exact evidence.** Never regenerate or widen a
   formatting, lint, type, duplication, architecture, or security baseline to
   hide a refactor regression.
10. **Fresh repository facts win.** Current Git, forge, CI, and owning specs
    outrank session narration. A clean worktree is not proof that work was
    delivered; an interrupted task is not proof that its commit was lost.

## Establish a reproducible baseline

Before planning or editing:

1. Identify the repository root, applicable instructions, default branch, and
   exact current upstream SHA. Preserve any pre-existing dirty state.
2. Record the measurement timestamp and distinguish `src/kiro_crew/`,
   `website/src/`, tests, generated static output, vendored code, fixtures,
   snapshots, migrations, and campaign tooling.
3. Inventory large files/classes/functions, duplicated policy, dependency
   cycles, public surfaces, owner tests, recent churn, active branches, and open
   PRs with path or intent overlap.
4. Treat a failed or throttled forge query as `UNKNOWN`. Fall back from GraphQL
   or search to REST plus ordinary Git refs/status data; never interpret an
   error or truncated page as an empty result.
5. Freeze the commands and exclusions used for metrics so later reports compare
   the same population.

Resolve this skill's directory to an absolute literal path before using a helper.
Run helpers with the interpreter Kiro Crew injects into the agent environment;
never substitute bare `python` or `python3`, which may be absent or point outside
the installed Kiro Crew runtime. The scanner is a reproducible first pass, not
an automatic task list. On POSIX:

```bash
"$KIROCREW_RUNTIME_PYTHON" "$SKILL_DIR/scripts/scan_refactor_hotspots.py" --root . \
  --path-prefix src/kiro_crew --path-prefix website/src \
  --format markdown --top 100
```

On PowerShell:

```powershell
& $env:KIROCREW_RUNTIME_PYTHON "$SKILL_DIR/scripts/scan_refactor_hotspots.py" --root . `
  --path-prefix src/kiro_crew --path-prefix website/src `
  --format markdown --top 100
```

Read [campaign-planning.md](references/campaign-planning.md) for a repository
scan, campaign reset, wave selection, or completion denominator.

## Design a module-scale wave

Rank scopes by structural payoff, contract risk, test strength, upstream churn,
active overlap, dependency centrality, platform sensitivity, and review cost.
Prefer stable, well-tested, low-overlap ownership boundaries before high-churn
central files.

For parallel work:

- Build a path-claim graph first. Every writer gets a disjoint path claim and an
  isolated Kiro Crew worktree based on the same upstream SHA.
- Give each task a stable title, owned paths, forbidden/shared paths, adjacent
  read-only areas, invariants, gates, and reporting contract.
- Reserve shared manifests, generated artifacts, locale catalogs, snapshots,
  baselines, registries, and central facades for one integration owner.
- Tell every worker what the other active scopes own.
- Limit writers by genuine path independence plus review and CI capacity. Do not
  fill every available slot merely because it exists.
- If isolation is unavailable, parallelize analysis only and serialize writes.

Read [worker-protocol.md](references/worker-protocol.md) before delegation. Use
[templates.md](references/templates.md) for Kiro Crew-specific briefs.

## Execute one scope

For every scope:

1. Reconfirm current upstream and open-change overlap immediately before work.
2. Map state ownership, entry points, public/dynamic/plugin surfaces, side
   effects, imports, patch targets, serialization, OS branches, and tests.
3. Run the smallest meaningful baseline test. Add characterization coverage
   when the behavior authority is implicit.
4. Choose dependency direction before moving methods. Extract cohesive owners,
   not one-file-per-method wrappers.
5. Keep an old surface as the single compatibility facade only when a real
   contract needs it. Move in-tree callers to the canonical owner.
6. Remove duplicate implementations, dead state, stale comments, and obsolete
   branches only after reachability and behavior are demonstrated.
7. Inspect the diff for accidental semantics, formatting churn, new cycles,
   forwarding sprawl, and changes outside the path claim.
8. Verify in increasing cost order and record exact evidence.

Read [module-execution.md](references/module-execution.md) before editing and
[validation.md](references/validation.md) before claiming a scope is green.

## Refresh and recover before publication

Fetch and compare again when a scope is locally green. Measure ahead/behind,
upstream commits touching the claim, open PR overlap, and whether upstream has
already implemented the intent.

On POSIX:

```bash
"$KIROCREW_RUNTIME_PYTHON" "$SKILL_DIR/scripts/audit_refactor_overlap.py" \
  --base <original-base-sha> --upstream origin/main --head HEAD \
  --path <owned-path>
```

On PowerShell:

```powershell
& $env:KIROCREW_RUNTIME_PYTHON "$SKILL_DIR/scripts/audit_refactor_overlap.py" `
  --base <original-base-sha> --upstream origin/main --head HEAD `
  --path <owned-path>
```

- **Low overlap:** rebase/replay and rerun affected gates.
- **Moderate semantic overlap:** compare contracts and integrate the coherent
  residual manually.
- **High overlap or partial supersession:** rebuild the smallest still-useful
  change from current `origin/main`; do not force an old large diff through.
- **Local-only work:** prove the commit and extracted behavior are absent
  upstream before replaying it.

Read [recovery-and-integration.md](references/recovery-and-integration.md).
When publication is authorized, load `prepare-pr` and follow its complete loop;
this skill never substitutes a lighter PR process.

## Measure the structural outcome

Track states separately:

`discovered -> baselined -> claimed -> designed -> implementing -> target-green -> integration-green -> review-ready -> merged`

Use `evidence-limited`, `parked`, `blocked`, `superseded`, or `abandoned` when
appropriate. Report both scopes touched and baseline hotspots eliminated. A
facade below a line threshold is not eliminated when the extracted owner remains
the same hotspot under a new filename.

For each landed scope record size/shape, canonical ownership, duplication,
coupling, public-contract evidence, navigability, package/runtime counter-costs,
the merge SHA, and residual work. Forecast from merged, verified hotspot
eliminations—not diff LOC, worker count, local commits, or open PRs.

Read [metrics-and-reporting.md](references/metrics-and-reporting.md) for campaign
status, percentages, forecasting, and true completion. The external research
behind the method, including the Hermes simplification and its counter-costs, is
in [research-synthesis.md](references/research-synthesis.md).

## Stop or redesign when

- current upstream or another active PR materially implements the same goal;
- conflicts cross undeclared ownership boundaries;
- behavior, public identity, side-effect order, or packaging equivalence cannot
  be demonstrated;
- the scope reaches an undeclared security, governance, harness, persistence,
  platform, or user-facing contract;
- required tests fail without a located cause;
- extraction increases cycles, indirection, facade complexity, or navigation
  cost without compensating payoff;
- progress requires a weaker test, wider baseline, ignored check, or hidden
  behavior change;
- local-only and stale work accumulates faster than review and integration can
  drain it.

Preserve recoverable work and report the evidence, remaining value, and next
safe action. Never discard dirty state, force-push, merge, close PRs, or delete
branches/worktrees without the required authority.
