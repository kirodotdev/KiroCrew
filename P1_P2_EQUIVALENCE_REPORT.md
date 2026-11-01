# P1-8 / P2-12 equivalence report — Kiro BYO 0.2.2

Baseline: the latest user-supplied `Kiro_BYO_0.2.2-source(1).zip`.

## P1-8 — provider/tool loop

The baseline `_run_provider_tool_loop` occupied lines 2715–3708 (994 source-line span plus signature context), with 10 explicit `continue` statements, one `step += 1`, and four `step -= 1` rewinds. The refactored orchestration method is 273 lines and still contains exactly 10 explicit `continue` statements, one increment, and four rewinds.

| Edge | Baseline branch | Step behavior | Refactored directive |
|---|---|---:|---|
| C1 | parallel subagents -> lead hand-back | `step -= 1` | `SUBAGENTS_HAND_BACK` |
| C2 | steer after provider interruption | advance | `STEER_AFTER_INTERRUPT` |
| C3 | worker ProviderError -> lead hand-back | `step -= 1` | `WORKER_FAILED_HAND_BACK` |
| C4 | pristine same-model retry | `step -= 1` | `RETRY_SAME_MODEL` |
| C5 | pristine fallback | `step -= 1` | `PRISTINE_FALLBACK` |
| C6 | worker findings -> lead | advance | `WORKER_FINDINGS_HAND_BACK` |
| C7 | queued steer after final candidate | advance | `QUEUED_STEER_AFTER_FINAL` |
| C8 | worker ExecutionGuard hand-back | advance | `WORKER_GUARD_HAND_BACK` |
| C9 | ExecutionGuard replan | advance | `EXECUTION_GUARD_REPLAN` |
| C10 | steer after tools | advance | `STEER_AFTER_TOOLS` |

The cross-round mutable variables are carried by `_ProviderLoopState`; per-invocation facts are carried by `_ProviderRoundContext`. The main loop retains the control-flow edges instead of hiding step rewinds inside helpers.

Safety/behavior checks retained from the latest baseline:

- pre-tool `tool_batch_admitted` durable checkpoint remains fail-closed (`critical=True`);
- cancellation/interruption recovery still preserves partial streamed text;
- provider same-model retry and pristine fallback happen only before visible/tool state;
- worker failure cannot fail the whole turn;
- ExecutionGuard warning/stop semantics and telemetry fields are unchanged;
- final PromptResponse token fields are unchanged;
- all current turn-runner, relay, subagent, recovery, and capability seam tests pass.

## P2-12 — declarative provider catalogue

Provider connection presets, transport labels, routing scores, and verified model capability facts now live in:

`src/kiro_byo/catalog/provider_catalog.v1.json`

Python retains loading, schema validation, matching, normalization, live-vs-curated merging, local overrides, and public query APIs. The matcher is declarative (`exact`, `prefix`, `suffix`, contains/exclude clauses, regex, `any`, `all`), so new model/family rows can be added without a Python source branch.

Every routing-score row includes `source.note`. Invalid bundled data fails during load with a precise path such as `provider_catalog.routing_rules[0].routing.speed`.

### Public API signatures checked unchanged

- `ProviderPreset`
- `transport_label(transport)`
- `official_routing_profile(preset_id, model_id)`
- `official_model_profile(preset_id, model_id)`
- `merge_official_model_profile(preset_id, row)`
- `provider_presets()`
- `provider_preset(preset_id)`
- `public_presets()`
- `public_platforms()`
- `credential_connection_profile(preset_id, credential)`
- `connection_identity(preset_id, base_url)`
- `saved_connection_identity(preset_id, base_url, product_id, profile_id="")`
- `connection_record(preset_id, base_url)`
- `product_base_urls(preset_id)`
- `normalize_reasoning_capability(...)`
- `normalize_tool_use_capability(row)`
- `model_routing_from_row(row)`
- `model_capability_from_row(row)`

A direct old-vs-new characterization comparison covered 2,888 provider/model profile and routing queries with zero differences. `provider_presets()`, `public_presets()`, and `public_platforms()` were also compared against the baseline payloads.

`ProviderPreset.public_dict()` now serializes only preset-owned data. Product/platform enrichment moved to `public_presets()`, removing the previous layering inversion without changing the public response.

## Latest-tree alignment fixes needed to make the supplied baseline testable

The uploaded baseline already contained newer production changes whose tests/release checks had not all been updated. These were aligned without reverting the production behavior:

- test doubles now use provider-neutral `tool_specs()` instead of removed `openai_tools()`;
- adaptive-routing tests/release check follow `task_profile_for_signals()` after its move to `mode_router.py`;
- durable-recovery test expects the newer recovery contention code `-32002`;
- stale `provider_admin.py` call `_official_endpoint_for_preset(...)` was corrected to the already-imported `official_endpoint_for_preset(...)`.

## Verification

- `python -m compileall -q src backend scripts`: pass
- `pytest -q`: **638 passed, 1 skipped, 42 subtests passed**
- `python scripts/release_check.py`: pass
- wheel includes `kiro_byo/catalog/provider_catalog.v1.json`: verified
- release/source runtime fingerprint: identical
