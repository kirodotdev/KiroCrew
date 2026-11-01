# Kiro BYO 0.2.5 — Architecture V3 / Transactional Installer Boundaries

**Date:** 2026-10-30  
**Input baseline:** `Kiro_BYO_0.2.5_Architecture_V2_Source.zip` (`0.2.5+a6b9a575ea1ed73d`)  
**Result:** `0.2.5+c4198c272ee96444`  
**Product version:** remains `0.2.5`

## Scope and intent

The primary target was `src/kiro_byo/integration_manager.py::install_from_app`, formerly **542 lines**. This iteration reduces high-risk change radius by extracting discrete phases, without changing the Crew integration contract, write/verification sequence, atomic-copy strategy, rollback protection, or Tiga / Dyna routing policy.

No Tiga/Dyna enablement, new execution semantics, UI changes, localized strings, or changes to `turn_runner.py` or `acp_server.py` are included.

## Code changes

1. `src/kiro_byo/integration_manager.py` — Installation flow now delegates to private helpers:
   - `_prepare_seam_install_plans`: enumerates source seams and aggregates the output plan (read-only preflight).
   - `_plan_created_seam`: distinguishes owned created files from foreign files and refuses foreign overwrite.
   - `_plan_generated_seam`: handles clean, patched, upgradeable and recovered partial generated seams.
   - `_resolve_owned_generated_baseline`: verifies pristine backups or uses a recorded base hash without guessing.
   - `_append_dashboard_install_plans`: validates dashboard patch assets and collects their planned outputs.
   - `_persist_install_baselines`: persists pristine baselines **after complete preflight**, as before.
   - `_verify_installed_seams`: checks exact committed content and required semantic closure before installing ACP.
   - `_commit_runtime_and_install_metadata`: copies runtime, removes stale owned runtime modules, writes bootstrap state / `.pth` / integration metadata **inside the original caller-controlled `try/except` rollback transaction**.

2. `tests/test_integration_install_boundaries.py` — Eight new focused contract tests for clean preflight, owned and absent baselines, foreign backup, unknown partial seam, foreign created file, byte mismatch and semantic commit mismatch. The pre-existing integration tests continue to exercise full transaction rollback and reinstall/uninstall.

3. `scripts/architecture_guard.py` and `config/architecture_budget.json` — Reduce >200-line Python function budget from 15 to **14**; add a **198-line cap** specifically for `integration_manager.install_from_app`. This prevents silently reinflating the transaction coordinator. `tests/test_architecture_guard.py` enforces the stricter budget.

## Measured before / after

| Metric | V2 | V3 |
|---|---:|---:|
| `install_from_app` size | 542 lines | **198 lines** |
| Python functions >200 lines | 15 | **14** |
| Python functions >100 lines | 54 | **54** |
| Largest Python function | `install_from_app` (542 lines) | `turn_runner._handle_prompt_request` (436 lines) |
| Product version | 0.2.5 | 0.2.5 |

The containing module has more small functions and may grow in total lines. The objective is separation of responsibilities and safer changes, not line-count cosmetics.

## Test evidence

- Complete `tests/test_*.py` collection partitioned into 9 independently executed batches (130 test files). All nine batches passed.
- After adding two final boundary tests, the affected test batch was rerun: **139 passed**.
- Consolidated final result: **1,148 passed, 1 skipped; additionally 58 unittest subtests passed**. No assertion failures.
- `python scripts/architecture_guard.py --check` — PASS.
- `python scripts/release_check.py` — PASS.
- `python -m compileall -q src backend` — PASS.
- `node --check ui/index.mjs` — PASS.
- SHA256 comparison with the V2 baseline confirms **unchanged**: `turn_runner.py`, `acp_server.py`, `mode_router.py`, `crew_patch_common.py`, Tiga and Dyna JSON Policy Packs, `backend/routes.py`, `ui/index.mjs`, and Crew overlay files.

A nine-batch run is not evidence that a separate single-process, uninterrupted pytest invocation can cleanly shut down. The earlier uninterrupted-run stall was not diagnosed in this iteration.

## Remaining limitations / risk

1. **No Windows/KiroCrew 0.11.0-rc.5 live installation performed.** Runtime liveness and integration activation must be checked on the actual host; Linux unit tests do not establish real Windows compatibility.
2. **Transaction semantics intentionally unchanged:** preflight passes before pristine backups are written, but persistent backup creation still happens before the temporary install transaction. In a failed later commit, an eligible newly created pristine backup may remain. It contains the verified original seam, not a fabricated recovery record; this behavior predates V3 and requires separate design discussion before changing.
3. Other hotspots remain, especially `turn_runner._handle_prompt_request` (436 lines). Tool invocation, side-effect uncertainty and Durable Run must not be split without rollback/permission fault-injection tests.
4. Dyna Flash/Strong/Miracle remains disabled and without finished independent policy definitions. This build does not claim live Dyna readiness.

## Deployment notes

Use the **App ZIP** for local KiroCrew update, and the **Source ZIP** for local version control/inspection. Back up active configuration and existing integrations before installing. KiroCrew Gateway restart may be required for newly installed patches to be active. See `BUILD_INFO.json` for build identity and unresolved platform verification flags.
