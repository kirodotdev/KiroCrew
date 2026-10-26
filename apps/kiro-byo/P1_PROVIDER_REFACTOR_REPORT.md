# P1-9 / P1-10 / P1-11 provider refactor report

Baseline: the latest optimized 0.2.2 source tree supplied immediately before this batch.
Version remains **0.2.2**.

## P1-9 — vendor policy outside `providers/`

### Before

- `config.py` contained Gemini/MiniMax historical endpoint branches.
- `provider_admin.py` duplicated official-endpoint logic and contained a Xiaomi credential-family branch.
- `account_status.py` selected DeepSeek/Moonshot/MiniMax behavior by `preset_id`.
- `provider_products.py` constructed Xiaomi/MiniMax product/profile topology through Python `if` chains.

### After

- `provider_catalog.v1.json` owns `legacy_base_url_migrations` and the loader exposes generic endpoint migration/query helpers.
- `provider_products.v1.json` owns special Platform/Product/Profile facts, credential prefix hints/rejections and account-handler identifiers.
- `provider_products.py` builds generic products and applies those data declarations; there are no Xiaomi/MiniMax/DeepSeek/Moonshot/Gemini branches in the generic product/admin/account layers.
- Provider-specific account endpoint/response parsing lives under `kiro_byo.providers.account_status`; the root `account_status.py` is a provider-neutral facade.
- `provider_admin.py` uses the single `official_endpoint_for_preset()` rule and catalogue-declared credential rejection messages.

Public Platform/Preset payloads were snapshot-compared before/after and are byte-structure equivalent as Python values.

## P1-10 — Responses API capability abstraction

`providers/responses_api.py` no longer branches on concrete response transport names.

The new `providers/capabilities.py` descriptor owns:

- store/cache-key policy;
- opt-in request headers;
- `previous_response_id` support;
- status codes that trigger stateless retry;
- reasoning wire style (`effort` vs `thinking-toggle`);
- provider-specific disabled reasoning sentinel;
- request fields stripped when reasoning is active;
- encrypted reasoning inclusion.

`providers/registry.py` now installs every Responses transport by iterating the capability registry instead of repeating eight bindings to the same module.

Existing transport round-trip/body tests pass unchanged.

## P1-11 — Mistral SSE duplication

`mistral_chat.py` changed from **402 lines to 223 lines**.

It now owns only:

- Mistral request-body policy;
- replayable structured content parsing;
- ThinkChunk/TextChunk stream merge;
- a `ChatContentCodec` descriptor.

The shared `chat_completions_common.run_native_chat_completion()` owns:

- request opening;
- interruptible response lifetime;
- non-stream JSON handling;
- SSE framing/parsing;
- usage parsing;
- tool-call delta accumulation;
- provider-context serialization;
- interruption semantics.

The codec is the only injection seam required for Mistral's structured assistant content.

## Regression guards

`tests/test_provider_boundary.py` now asserts:

1. `responses_api.py` contains no provider-type literal branching;
2. Mistral delegates to the shared Chat engine and contains no private SSE loop;
3. generic config/admin/product/account modules do not branch on vendor preset ids.

## Verification

- `python -m pytest -q` -> **641 passed, 1 skipped, 42 subtests passed**
- `python -m compileall -q src backend` -> PASS
- `python scripts/release_check.py` -> PASS
- Public `public_platforms()` and `public_presets()` payloads -> unchanged from pre-refactor snapshot
