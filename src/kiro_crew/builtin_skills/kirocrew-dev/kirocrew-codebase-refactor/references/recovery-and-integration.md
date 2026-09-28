# Kiro Crew Refactor Recovery and Integration

This reference belongs to `kirocrew-codebase-refactor` and applies only to the
Kiro Crew source repository or one of its worktrees. Read it for stale branches,
interrupted work, upstream overlap, or the handoff into `prepare-pr`.

## Refresh twice

Refresh upstream and overlap:

1. immediately before implementation; and
2. immediately before publication or integration.

Record base, upstream, and head SHAs; ahead/behind counts; owned paths changed
upstream; active path overlap; and intent overlap. Inspect the local branch, its
remote branch, and its PR independently. A clean local worktree proves none of
the other states.

Use the bundled overlap helper with Kiro Crew's injected runtime for local Git
evidence. Forge inspection remains necessary for active PR intent overlap. On
POSIX:

```bash
"$KIROCREW_RUNTIME_PYTHON" "$SKILL_DIR/scripts/audit_refactor_overlap.py" \
  --base <original-base-sha> \
  --upstream origin/main \
  --head HEAD \
  --path <owned-path>
```

On PowerShell:

```powershell
& $env:KIROCREW_RUNTIME_PYTHON "$SKILL_DIR/scripts/audit_refactor_overlap.py" `
  --base <original-base-sha> `
  --upstream origin/main `
  --head HEAD `
  --path <owned-path>
```

When GraphQL or a forge search endpoint is throttled, use REST plus ordinary Git
refs and check APIs. Validate status codes and pagination. A failed or partial
query is `UNKNOWN`, never “no overlap.”

## Classify drift

### Low overlap

- Owned paths are unchanged or only mechanically changed upstream.
- Rebase or replay onto the latest `origin/main`.
- Rerun affected target and integration gates.

### Moderate semantic overlap

- A small number of upstream changes affect the owned contract.
- Compare behavior and owning specs commit by commit.
- Resolve manually. Never accept all “ours” or “theirs” across the scope.
- Re-establish characterization evidence for the overlapping behavior.

### High overlap or partial supersession

- Upstream changed the ownership boundary or extracted the same responsibility.
- Stop rescuing the old diff.
- Inventory what current `origin/main` already supplies.
- Recreate only the cohesive residual value in a fresh worktree.

### Local-only interrupted work

- Verify the commit, checkout, and dirty state directly.
- Confirm the extracted modules or behavior are absent upstream.
- Audit overlap before modifying the old branch.
- Prefer a fresh-base residual rebuild over forcing a stale large diff through
  broad conflicts.

Session status is a lead, not proof. An interrupted agent may have a valid commit;
a completed agent may have no deliverable change.

## Drain before another wave

Classify every existing scope as:

- merged/delivered;
- active and fresh;
- local-only but salvageable;
- stale and requiring a residual rebuild;
- superseded; or
- explicitly abandoned with evidence.

Drain means integrate, rebuild, park with an exact handoff, or abandon with the
required authority. Starting more writers does not resolve orphaned work.

## Hand off publication

Once the refactor is locally green, load `../prepare-pr/SKILL.md`. That sibling
owns the commit, base sync, single-commit policy, body accounting, local review,
push, PR creation/update, dispositions, and PR-to-green loop.

Carry these refactor-specific facts into its PR body and reviewer briefs:

- frozen and current upstream SHAs;
- ownership before and after;
- exact public behavior preserved;
- compatibility facade or identity mechanism;
- structural metrics and counter-movers;
- package, platform, parity, and test evidence;
- active overlap audit;
- residual hotspots and explicit non-goals.

Do not advertise diff churn as simplification. File moves and extracted tests can
make a sound refactor add lines while reducing cognitive load.
