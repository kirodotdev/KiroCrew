# Kiro BYO 0.2.5 — Architecture V4 / Prompt Routing & Turn Boundary

**Date:** 2026-10-30  
**Baseline:** `Kiro_BYO_0.2.5_Architecture_V3_Source.zip` (`0.2.5+c4198c272ee96444`)  
**Product version:** stays `0.2.5`  
**Build ID:** `0.2.5+e22e6f7db3d1cfe6`  
**Host target:** KiroCrew `0.11.0-rc.5` (not physically verified on Windows)

## Scope

This release limits work to the ACP prompt entry point and Type/concrete-model route preparation. It does **not** enable Dyna, modify any Tiga/Dyna Policy Pack, change Provider adapters or tool privileges, redesign the UI, alter the Durable Run Ledger, or modify Crew overlays.

## Changes

- `src/kiro_byo/turn_runner.py`
  - `_handle_prompt_request` now owns session identity checks, in-process and cross-process turn locking, response buffering, and terminal response flush. The lock/finally release order is preserved.
  - `_execute_prompt_request_locked` handles the admitted turn: prompt decode, echo and compaction compatibility, routing preparation, input capability checks, user checkpoint persistence, durable admission, real route-event emission, and entry into the existing provider/tool loop.
  - `_rewind_durable_prompt` encapsulates the existing recovery revalidation and state rewind under the acquired session turn lock. Recovery still denies stale/unsafe runs before rewinding.
- `src/kiro_byo/prompt_routing.py` (new)
  - `_prepare_prompt_routing` prepares Crew tool visibility and applies language preferences.
  - `_select_type_prompt_route` invokes the pre-existing router and execution-plan compiler; it does not implement new Type policy.
  - `_restore_prompt_type_pin` accepts a pinned model only if present in the current execution fallback pool.
  - `_select_concrete_prompt_route` preserves concrete-model bypass and per-model thinking preference.
  - `_PreparedPromptRoute` carries the chosen provider, model and execution plan back to Turn Runner. No Provider call or Durable Run starts from this module.
- `scripts/release_check.py` now verifies the concrete-model bypass event in its actual new module, as well as the Turn Runner mixin connection. This is not a disabled or weakened release check.
- `config/architecture_budget.json` and `tests/test_architecture_guard.py` ratchet function size and file limits without raising existing caps.
- `tests/test_prompt_routing_boundary_v4.py` adds fault-injection cases for route rejection, pre-provider checkpoint failure, and sticky-pin eligibility.

## Structural metrics

| Metric | V3 | V4 |
|---|---:|---:|
| `_handle_prompt_request` | 436 lines | 92 lines |
| `turn_runner.py` | 4,957 | 4,817 |
| `prompt_routing.py` | — | 240 |
| Python functions over 200 lines | 14 | 13 |
| Python functions over 100 lines | 54 | 54 |

An additional 240-line module replaces a large routing block, so project-wide line reduction is **not** claimed. The benefit is smaller reviewable execution/selection boundaries and explicit guards against regrowth.

## Tests / verification

- Full `tests/test_*.py` collection: **131 files** in **11 independent pytest batches**.
- **1,152 passed, 1 skipped, and 58 unittest subtests passed.** All 11 batches returned process status `0`; no test assertion failures.
- Focused routes, ACP lifecycle, Durable Recovery, Tiga/Dyna policy preflight, session fencing, routing/health regressions and the three new fault-injection tests passed.
- `python scripts/architecture_guard.py --check` — PASS.
- `python scripts/release_check.py` — PASS.
- `python -m compileall -q src backend` — PASS.
- `node --check ui/index.mjs` — PASS.
- Build release ZIP is a runtime subset of the complete source ZIP; installer and source runtime fingerprints are checked by the packaging script.

## Remaining risk / non-goals

1. **No Windows/KiroCrew 0.11.0-rc.5 integration E2E performed**. Windows ACP process startup, Crew host patch live behavior, actual app update and gateway restart still need verification on the host.
2. No real external Provider/API requests were issued. Tests use local Provider mocks and simulated routes, so real Provider network behavior may differ.
3. Dyna Flash/Strong/Miracle remain disabled and are **not** feature-complete. The mode-neutral routing seam is tested without asserting Dyna is ready to launch.
4. Session and tool side-effect safety relies on existing Durable Run and execution guard code, not a newly introduced transactional framework. This iteration deliberately keeps tool, ledger and ACP semantics unchanged.
5. Large functions remain (such as `_execute_native_tool` and `_run_provider_tool_loop`). Future work should add targeted fault-injection and recoverability tests before splitting their mutating execution paths.
6. Full test collection was executed in 11 isolated batches rather than one uninterrupted pytest process; this does not establish that a single combined process exits cleanly.

## Deployment

Use the V4 **App ZIP** for local KiroCrew update and the **Source ZIP** for source control. Back up active state and configuration and verify the installation in a noncritical Crew workspace first. The product SemVer remains `0.2.5`; identify this revision by `BUILD_INFO.json::build_id` and the SHA256 checksum file.
