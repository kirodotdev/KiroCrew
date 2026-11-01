# Kiro BYO 0.2.5 — Architecture V5 / Native Tool Dispatch Boundary

**Date:** 2026-10-30  
**Baseline:** `Kiro_BYO_0.2.5_Architecture_V4_Source.zip` (`0.2.5+e22e6f7db3d1cfe6`)  
**Product version:** remains `0.2.5`  
**Target host:** KiroCrew `0.11.0-rc.5` (Windows E2E **not verified**)

## Goal and observed concern

`TurnRunnerMixin._execute_native_tool` contained 312 lines combining tool argument validation, Saved Agent visibility, local policy, scope approval, preflight, Crew approval, interruption control, tool invocation and audit. These are different responsibilities with different safety boundaries. The repeated `allow_once` revocation and interruption fences must be preserved, not removed for the sake of code shortening.

This is an incremental responsibility split, **not** a rewrite of native execution or Durable Run. No independently demonstrated production-security bug is claimed to have existed before the refactor. One temporary refactor-only return-shape regression was caught by a new injected cancellation/steer test and fixed before release.

## Changes

- `src/kiro_byo/native_tool_dispatch.py` (new): `NativeToolDispatchMixin` owns the previous native tool-call dispatch. `_execute_native_tool` is 183 lines; `_approve_native_scope` (82 lines) handles narrow Crew directory grants; `_run_authorized_native_invocation` (75 lines) runs and audits an authorized invocation with `finally` revocation for one-time grants.
- `src/kiro_byo/turn_runner.py`: inherits the new mixin; the call site remains `self._execute_native_tool`. Its provider/tool batch coordinator, Durable Run checkpoints and Mode routing are unchanged.
- `tests/test_native_dispatch_boundary_v5.py`: nine fault-injection and authorization/batch tests cover success and exception scope cleanup, cancellation/steering fences, preflight failure, static policy rejection, mid-batch cancel and duplicate call ID blocking.
- `scripts/release_check.py`: structural AST check for the Turn Engine mixin registration and safety markers in the native dispatch module; does not waive prior routing/durable checks.
- `config/architecture_budget.json`: cap over-200-line functions from 13 to 12; cap native dispatch entry to 183 and core runner to 4,504 lines.
- `CHANGELOG.md` and this report: audit trail for this release.

## Behavior and safety invariants

1. Native policy `DENY` or Saved Agent tool visibility rejection stops before permission prompts.
2. Out-of-scope filesystem access always requires host permission for the exact call; grant occurs only after a final pre-dispatch interruption check.
3. `allow_once` roots are revoked after execution, during preflight failure and after interruption between preflight and execution; `allow_always` remains only within session runtime as before.
4. Cancellation and steering are checked after approval and before invocation. A running tool may have side effects even if later cancelled; no rollback or guarantee of exactly-once semantics is introduced.
5. Tool `status`, `audit.record` and `tool.call.completed` carry the original result semantics. Batch duplicate IDs and cancellation are still handled before later dispatches.
6. Durable Run admission, tool batch receipts, MCP dispatch, Provider calls and Tiga/Dyna mode selection are not modified.

## Metrics

| Metric | V4 | V5 |
|---|---:|---:|
| `_execute_native_tool` | 312 | 183 |
| `turn_runner.py` | 4,817 | 4,504 |
| Python functions > 200 lines | 13 | 12 |
| Python functions > 100 lines | 54 | 54 |
| `native_tool_dispatch.py` | — | 361 |

## Tests

- **132 test modules**; all verified in isolated pytest batches. Group 5 was divided into two six-file commands after one combined process stopped responding.
- **1,161 passed, 1 skipped, plus 58 unittest subtests passed.** No assertion failure in completed groups. One initial refactor-only test failure (`_approve_native_scope` returning an inconsistent tuple on cancelled/steered grant) was fixed, and its tests re-passed.
- `scripts/release_check.py`: PASS.
- `scripts/architecture_guard.py --check`: PASS.
- `python -m compileall -q src backend`: PASS.
- `node --check ui/index.mjs`: PASS.

## Limits and outstanding risks

- No physical Windows/KiroCrew `0.11.0-rc.5` end-to-end test, ACP host restart verification or real Provider API request has been performed.
- No cross-process exactly-once execution guarantee is introduced. Durable side-effect uncertainty remains conservative and must not trigger automatic replay.
- The native execution path still contains safety-sensitive effects; its 183 lines are a reviewable unit, not mathematically proven equivalent through tests.
- Other long functions (`_run_provider_tool_loop`, `integration_manager`, UI generator methods) remain under incremental architecture budgets. Future refactors should use fault injection before moving further control-flow blocks.
- Dyna remains disabled, and its Flash/Strong/Miracle strategy is not completed by this infrastructure change.
