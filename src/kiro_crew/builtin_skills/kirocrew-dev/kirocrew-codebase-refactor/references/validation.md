# Validation and Equivalence Evidence

This reference belongs to `kirocrew-codebase-refactor` and applies only to the
Kiro Crew source repository or one of its worktrees. Repository-native commands,
the owning specs, `writing-tests`, and `prepare-pr` outrank generic examples.

Read this reference before claiming a scope is green, review-ready, or complete.

## Fresh evidence gate

Every positive claim requires evidence from the current code state:

1. Name the command or comparison that proves the claim.
2. Run it completely on the relevant HEAD.
3. Read the exit code and full failure/skip summary.
4. Check that the command actually covers the claimed property.
5. State the result with its scope and timestamp/HEAD.

Previous runs, a worker's confidence, a linter passing, or “the change is mechanical” are not substitutes. If a class of evidence is unavailable, say so and narrow the claim.

Distinguish `observed`, `executed`, `user-reported`, `inferred`, and `unavailable` evidence. Seeing test source is not an executed test result; seeing a build configuration is not a successful build.

## Evidence matrix

Build a scope-specific matrix before implementation:

| Contract | Baseline evidence | Post-change evidence | Required? |
|---|---|---|---|
| Inputs/outputs | Characterization/unit tests | Same tests | Yes |
| Exceptions/errors | Error-path tests/snapshots | Same types/messages/policy | As applicable |
| Side effects/order | Integration trace or mock call order | Equivalent trace | As applicable |
| Public imports/exports | Symbol/import manifest | Old and new paths resolve | As applicable |
| Identity/patch seams | `is`/reference and monkeypatch tests | Same observable target | As applicable |
| Serialization/wire | Golden bytes or normalized structure | Exact parity | As applicable |
| Config/defaults/help | Captured artifact | Exact parity | As applicable |
| Packaging/install | Built artifact/import smoke | New modules included | Yes when files move |
| Runtime/performance | Reproducible benchmark | Within stated budget | For hot paths |
| Platform behavior | Platform or contract tests | Required targets pass | For OS/FFI work |

Prefer deterministic artifacts and manifests over prose assurances.

## Layered verification

Run cheap, specific gates first, then broaden:

1. syntax/import smoke for changed modules;
2. direct tests for extracted units;
3. characterization and compatibility tests for the old surface;
4. owner/module suite;
5. formatter, linter, type checker, duplication, architecture, security, and baseline ratchets applicable to changed files;
6. build, package, install, and entry-point smoke;
7. broader repository suite;
8. relevant platform/live tests;
9. remote required checks and review state.

Repository-native commands outrank generic substitutes. A broad suite does not replace a missing contract test if the contract is not exercised.

## Test integrity

- Do not weaken assertions, delete coverage, increase timeouts, add broad skips, or rewrite expected output merely to accommodate the refactor.
- Tests may move or change internal imports when internal layout changes, but behavioral assertions must remain equivalent or stronger.
- New tests should pin the compatibility risk: public names, order, defaults, wire data, packaging, plugin access, platform ABI, or failure semantics.
- For a new regression/contract test, perform red-green verification when practical: confirm it fails when the relevant preservation/fix is removed, then restore the change and confirm it passes.
- Mutation testing is a valuable pre-PR gate for logic-heavy changes when the repository supports it; otherwise record why it is not meaningful and use alternate evidence.

## Baseline and inherited failures

When a gate fails:

1. Determine whether the failure touches owned code or a declared invariant.
2. Rerun only enough times to diagnose a plausible flake; keep a fixed bound.
3. Reproduce on a clean checkout of the same upstream SHA with the same command and environment.
4. Compare failing tests/files and failure details, not just total counts.
5. Classify:
   - owned regression;
   - inherited upstream failure;
   - confirmed flaky;
   - infrastructure/environment;
   - required blocker;
   - advisory/non-required.

“Unrelated-looking” is not evidence. Do not repair an inherited failure inside the scope unless explicitly asked; document it and follow repository policy.

## Frozen-surface parity

For high-risk refactors, capture and compare stable artifacts from baseline and branch:

- exported/public symbol manifest and signatures;
- CLI `--help`, command list, and exit behavior;
- tool or API schemas with stable ordering/normalization;
- system prompts/templates where exact text is contractual;
- configuration defaults and precedence;
- serialized JSON/protobuf/wire fixtures;
- SQL/DDL and migrations;
- localized user-visible strings;
- plugin/entry-point discovery;
- platform ABI declarations and argument/return types.

Use byte equality when byte order/text is contractual; otherwise use a canonical structural comparison. Record intentional differences separately.

## Packaging and compatibility traps

When adding modules:

- build the real package/artifact;
- inspect its contents or install it into a fresh environment;
- import every public entry point and representative moved symbols;
- exercise dynamic plugin/entry-point discovery;
- verify old compatibility paths and new canonical paths;
- ensure in-tree code does not accidentally depend on temporary shims;
- test editable and packaged modes if they resolve modules differently.

Source-tree tests can all pass while a wheel, bundle, archive, or installer omits extracted files.

## Counter-metrics

A structural refactor can improve readability while worsening another dimension. Measure relevant counter-movers:

- module/file count;
- import/dependency edges and cycles;
- startup/import/build time;
- memory/resource use;
- indirection/facade depth;
- public surface area;
- test collection/runtime;
- package size;
- agent navigation token density.

State the tradeoff rather than hiding it. Improvement is a reasoned balance, not a single optimized number.

## Independent adversarial review

For large or high-risk diffs, use a read-only reviewer that did not implement the change. Ask it to find behavior changes, missing public symbols, changed exception semantics, packaging omissions, compatibility leaks, test weakening, and counter-metric regressions. Give it the baseline contract and diff, not the implementer's conclusions.

The coordinator validates findings and reruns decisive evidence. Reviewers do not mutate the implementation checkout.

## Evidence report

Report:

```text
HEAD/base:
Changed scope:
Targeted tests: command, exit, pass/fail/skip counts
Compatibility/parity: artifact and result
Static gates: command and result
Build/package/import: command and result
Broader suites: command and result
Remote required checks: current snapshot
Inherited/flaky/advisory failures: reproduction evidence
Unavailable evidence:
Known counter-movers:
Conclusion: exact state, not “done” shorthand
```
