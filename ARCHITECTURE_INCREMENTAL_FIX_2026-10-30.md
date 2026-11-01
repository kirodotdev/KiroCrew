# Kiro BYO 0.2.5 — ACP Content Boundary / Architecture Ratchet (2026-10-30)

## Baseline and scope

Input: `Kiro_BYO_0.2.5_Dyna_Preflight_Source.zip`, runtime build `0.2.5+b40211a600b91df2`.
Product version stays **0.2.5**. Dyna remains **disabled**. No Tiga/Dyna Policy Pack changes, model selection changes, integration patch changes, tool/permission behavior changes, storage format changes, UI changes, or translation changes.

## Verified improvements

1. `src/kiro_byo/acp_content.py`: new dependency-light ACP prompt codec. This contains the original logic for text, image, resource links, embedded resource diagnostics, and data-URL reconstruction used for durable recovery. Pure functions, no mode/provider/trace/session imports.
2. `src/kiro_byo/acp_server.py`: old `prompt_text`, `prompt_message`, `_prompt_blocks_from_user_message` remain available with the same signatures. They now delegate directly to the codec. ACP control plane drops from **3,709 to 3,624 lines** without moving sessions, I/O, permission boundaries or Durable Run transactions.
3. `tests/test_acp_content_codec.py`: tests mixed content, order preservation, baseline resource links, embedded text/image and unsupported binary reporting, saved user-message recovery, and delegation. AST comparison to the preflight baseline verified exact executable method body preservation, excluding indentation-only changes in docstrings.
4. `scripts/architecture_guard.py`, `config/architecture_budget.json`, `tests/test_architecture_guard.py`: a standard-library code-size ratchet in the test suite. It reports the most complex Python functions and fails if existing hotspots grow beyond the tracked budget. When a verified refactor reduces these limits, lower the budget with it; increasing the budget requires deliberate review.

## Measured complexity after changes

- `turn_runner.py`: 4,957 lines (unchanged).
- `acp_server.py`: 3,624 lines (previously 3,709).
- Python functions exceeding 100 lines: 54, exceeding 200 lines: 15 (including `src/kiro_byo`, backend, scripts; excluding tests and integration overlays).
- Longest Python function: `integration_manager.install_from_app` — 542 lines.

The total project line count is **not expected to decrease**: logic moved into its own module, and new tests/guardrails deliberately add code. The success criterion is reducing coupling and blocking unreviewed complexity growth.

## Regression results

- Targeted content/ACP/Dyna and architecture guard tests: PASS.
- Entire 129-file pytest collection executed across five file groups, with the third group split into two smaller executions to avoid an unrelated process-shutdown stall: **1,140 passed, 1 skipped**, plus **58 unittest subtests passed**. No assertion failures.
- `python scripts/release_check.py`: PASS.
- `python scripts/architecture_guard.py --check`: PASS.
- `python -m compileall -q src backend`: PASS.
- `node --check ui/index.mjs`: PASS.
- Baseline executable AST equivalence for each of the three moved ACP functions: PASS.

The single uninterrupted pytest run did not finish within the execution window, so the complete coverage claim refers to the explicitly file-partitioned runs. This is a test runner/shutdown issue needing separate diagnosis; it is **not evidence** of functional success for the uninterrupted run.

## Unresolved / next priorities

- Large transactional functions remain. Next structurally valuable boundary: extract ACP model/options projections or TurnRunner read-only view generation behind a compatibility shim, with golden snapshots and failure-path coverage. Do **not** blindly split the tool/Durable/unknown-side-effect transaction.
- Dyna Flash/Strong/Miracle still lack released independent execution/collaboration safety policy; preflight tests remain simulations, not proof of live Dyna readiness.
- KiroCrew v0.11.0-rc.5 Windows integration/liveness and UI must be verified on a real host. No such verification was performed in this environment.
- The architecture ratchet tracks complexity size, not dependency cycles, race conditions or execution correctness; keep behavior tests as the primary safety gate.
