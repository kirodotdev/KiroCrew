# Kiro Crew CI at a glance: issues and lanes

Moved verbatim from the kirocrew-prepare-pr `SKILL.md` core. Full lane detail is in `ci.md`.

### Issue first: triage and tiers

Every PR names an issue of this repository on a line of its own: `Closes #N` (the
merge closes it) or `Part of #N` (it stays open). The `Issue Gate` lane checks it.
It is paused today (`GATE_ENFORCED: "false"`); write the line anyway.

No issue yet? Open one BEFORE Phase 1, drafted by `feature-request` step 4. The tier
table (T1-T4; T3 and T4 wait for `triaged`) and the Captain's triage are in
`references/ci.md` § Issue tiers.

### Lanes

`PR Readiness` is the one required status: it folds every lane into one verdict
and one `readiness:` label. The real merge gate is a human approval plus
`PR Readiness`. Fix a red `Fast Gate` first: the heavy matrix and the fork AI
lanes wait on it. For any other lane (what it checks, if it blocks, how to
clear it, fork PRs, the merge queue), read `references/ci.md`.

## Review-ready when the diff touches a test

When the diff adds or changes a test, review-ready also means the body carries that
test's determinism proof: the repeats and a shuffled order, and for a fix to a flaky
test the forced condition red on the parent and green on the fix (testing-conventions
§ Proving a determinism fix). `AUTOSDE.yaml`'s `tests-are-deterministic` rule is what
review holds the test itself to.
