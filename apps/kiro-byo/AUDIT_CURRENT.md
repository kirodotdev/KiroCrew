# Kiro BYO 0.2.0 Release Audit — Product / Runtime Truth Boundary

Date: 2026-10-16

## 1. Scope and lineage

0.2.0 productizes the runtime accumulated through the 0.1.7 line; it does not introduce a second execution engine. Deterministic adaptive routing, Policy Pack schema-v4 orchestration, canonical session events, bounded internal execution and durable Run recovery remain runtime-owned. The main 0.2.0 change is the management/product surface and the evidence projected into it.

## 2. Final management information architecture

The normal App navigation is **Overview / Providers / Models / Modes / Accounts / Records / Settings**. There is no Tasks page, standalone Integration page, external-Agent management page or workspace diff/apply management page. KiroCrew integration and ACP conformance are Settings sections. Internal Agent/Worker/Reviewer roles are runtime implementation details, not required product vocabulary.

## 3. Frontend/backend truth contract

The UI must not manufacture runtime facts. Provider rows use **configured** unless a real health result exists; model counts use **discovered** rather than **currently available** unless availability was actually verified. Modes reads orchestration semantics from the backend Policy Pack projection rather than a second hard-coded JavaScript policy table. Recent task-shape percentages are read from real `model.route.selected.signals`; missing signals render as unknown. Overview does not synthesize an execution role when only a durable Run is known.

`scripts/release_check.py` supplements JavaScript syntax checking with a product contract gate: referenced `*Page` components must exist, static UI API calls must correspond to registered App routes, removed workspace/external-agent management surfaces must not leak back into the UI, and editor/patch backup artifacts fail the release.

## 4. Records / observability boundary

The user-facing surface is **Records**, with All / Requests / Routing / Recovery views. TurnTelemetry remains a bounded metadata-only per-turn summary. RuntimeTrace remains a lower-level bounded event stream and is loaded lazily when Records opens; raw event payloads are behind drill-down details rather than a permanent debug-console tab. Durable Runs remain the recovery authority. These stores have distinct responsibilities and are not presented as interchangeable sources of truth.

Canonical conversation/session state remains the append-only session event model. Telemetry/trace are observability projections and durable Runs are recovery/control-plane state; none is allowed to rewrite canonical conversation history.

## 5. Routing evidence

Adaptive ranking remains deterministic and evidence-bounded. Real recent latency/reliability evidence may adjust eligible-model ranking after capability gates; Quality/Economy remain catalogue/policy priors. `routing_dashboard` now projects the actual multi-dimensional route signals (`complexity`, `breadth`, `parallelizability`, `risk`, `tool_intensity`, `write_intensity`, `context_demand`, `verification_value`, `latency_sensitivity`) rather than exposing only complexity and inviting the UI to fill missing values.

The selected Type's recent model usage uses a 60-hour window. No Router LLM, random exploration or user-editable routing weights are introduced.

## 6. Provider architecture / anti-sprawl guard

Zhipu, Moonshot/Kimi, Qwen, Xiaomi MiMo and Mistral are thin request/content-policy adapters over the shared native Chat completion/SSE engine in `chat_completions_common.py`; Mistral injects only its structured ThinkChunk codec. Responses-family differences are capability records in `providers/capabilities.py`, and commercial product/profile/account strategy differences are catalogue data rather than generic-layer vendor branches. Tests and release checks guard these boundaries.

Other genuinely different wire protocols (Responses API, Anthropic Messages, Gemini Interactions, Mistral/Cohere specifics) remain separate adapters because collapsing incompatible protocol semantics would create a false abstraction.

## 7. Removed management surface / retained runtime capability

The App backend no longer exposes unused workspace diff/apply or external-ACP-Agent CRUD/test management routes after those product pages were removed. The underlying runtime modules are retained where ACP execution, isolation, compatibility or recovery contracts still consume them. This is deliberate: product-surface removal is not used as an excuse to delete runtime behavior that 0.1.7 established and tests still verify.

The bounded Records projection now returns only the summary, turn rows, durable runs and retention limit needed by the product UI; it no longer loads unrelated Agent projection/workspace/catalog data every refresh.

## 8. Source/release hygiene

Release/source archive generation excludes caches, mutable state and editor/patch backup files (`.bak`, `.backup`, `.orig`, `.rej`, `~` variants). Release checks independently reject these artifacts if they are present in the audited tree. Mutable session/telemetry/runtime-trace/run/workspace state and configured credential/provider files remain excluded from shipping archives.

## 9. Remaining structural debt (not refactored inside this UI audit)

The former `_run_provider_tool_loop` and code-shaped provider catalogue have now been refactored behavior-preservingly: cross-round state lives in `_ProviderLoopState`, the ten historical continue edges remain explicit, and provider/model facts live in the versioned declarative `catalog/provider_catalog.v1.json`. `acp_server.py` and `integration_manager.py` remain large and should receive the same regression-led treatment later; do not collapse their session/request/persistence or integration transaction boundaries merely to reduce line count.

The UI is also still a single `index.mjs` while its layout is actively changing. It should be split into pages/components/styles only after the current information architecture stops moving; splitting it during the design pass would increase merge/edit churn without reducing runtime risk.

## 10. Verification

The release gate covers version consistency, Policy Pack shape, session-event persistence, reasoning/thinking boundaries, adaptive routing, internal execution/durable recovery contracts, UI/backend route alignment, shared same-wire provider transport, source hygiene and mutable-state exclusion. Final 0.2.0 verification completed with **614 passed, 1 skipped, 42 subtests passed**, `node --check ui/index.mjs` clean, Python bytecode compilation clean, and `scripts/release_check.py` passing. The final source and release archives are built only from this audited tree.
