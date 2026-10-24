# Kiro BYO 0.1.0 — Final Architecture & Edge Audit

Date: 2026-10-14

## Scope and baseline

The continuation work started from the last complete pre-audit archive `Kiro BYO 0.1.0-final-short-ui.zip`, whose frozen audit baseline passed 396 tests + 38 subtests. Batch 07 starts from `Kiro_BYO_0.1.0-batch06-tasks11-12.zip` (SHA-256 `afc665478dce1408627a6422373ddccf9a0d2b4a02d800692cd4d747848cf4e3`) and performs the final two planned tasks: a whole-tree edge-case audit followed by release regression/architecture comparison. The product version intentionally remains **0.1.0**.

## Edge cases fixed in the final audit

| Surface | Failure mode found | Final behavior |
| --- | --- | --- |
| Provider defaults | Refresh/deletion could leave `provider.default_model` or root `default_model` pointing at a disappeared model/provider | The nearest valid concrete default is repaired atomically; a missing root default is not invented for a fresh Type-routed setup |
| Mode vs Model | Changing Mode could erase concrete-model default preferences even though Mode/Type and Model are separate axes | Mode mutation changes only Mode state; explicit model preferences survive |
| Commercial identity | Persisted `platform_id/product_id/profile_id` could become stale after an official endpoint/region/plan change | A recognized official endpoint is authoritative for Product/Profile identity; arbitrary relays retain explicit saved identity |
| Concurrent admin writes | Threaded UI requests used independent read-modify-write cycles and could overwrite each other | All config mutations share a re-entrant process lock and unique fsynced atomic temp writes |
| Background catalogue refresh | A slow refresh could replace a newer explicit user edit with an older snapshot | Refresh uses compare-before-replace and aborts on concurrent file change |
| Session filenames | Sanitized IDs such as `a/b` and `a?b` could collide; fixed temp names could race | Persisted names include a digest of the original id; writes use per-write unique temps and are ordered by the session lock |
| Legacy sessions | Changing the durable filename scheme could orphan previous sessions | New loader/deleter transparently checks the old sanitized filename as a compatibility fallback |
| Product evolution | A future plan required code-side credential-prefix branching | Product metadata can declare credential prefixes and route to a Profile while reusing the installed protocol adapter |
| Metadata trust | A new Platform overlay could attempt to name an arbitrary executable adapter | New platform products must reuse a code-owned preset/transport pair; metadata cannot mint wire code |
| Type picker | The caption still encoded Tiga/Multi as a CSS product assumption | Group caption is generated from the installed Policy Pack's declared default Type; Tiga still renders “默认复合型” |

## Intentional hard-codes that remain

The final search did not mechanically delete every vendor/model/policy name. The remaining cases are classified rather than hidden:

1. **Built-in Tiga release contract.** `scripts/release_check.py` deliberately checks the shipped Tiga pack and its four built-in Types. Runtime routing and picker enumeration remain Policy-Pack driven; the release gate is allowed to assert what this particular bundle promises to ship.
2. **Curated model quirks/fallback facts.** Known context limits, thinking modes, effort levels and compatibility hints may still be curated where providers do not expose them. They are not generation allowlists: explicit live discovery facts win, unknown remains unknown, and local overrides remain possible.
3. **Weak vocabulary hints in task complexity.** The multilingual keyword regex remains only a low-weight hint. Structural request signals dominate and regression tests cover equivalent multilingual substantive tasks.
4. **Responses/Gemini request ownership.** `responses_api.py` and `gemini_interactions.py` intentionally open their first request locally because HTTP status is part of continuation-expiry recovery. This is protocol state semantics, not duplicated commercial-provider plumbing.
5. **Legacy Xiaomi Token Plan alias validation.** The hidden compatibility alias still rejects the wrong credential family. New product routing itself is metadata-driven.

No old un-namespaced `kiro-byo-type:sky|multi|power|glitter` wire ids were found in executable product surfaces.

## Architecture after the seven batches

### Routing / Type boundary

- Multi is the default Tiga Type; Sky/Power/Glitter are explicit user picker choices.
- The assistant has no setter/tool that can mutate the session Type. It can discuss or suggest a manual choice naturally; actual switching is a KiroCrew control-plane action.
- `tools_available` and current-turn `tools_required` are separate signals.
- Multi's complexity estimate is primarily structural rather than language-keyword driven.
- Glitter derives reasoning and worker-model selection from each concrete gathering subtask.
- Power and Glitter strong-tier contracts fail closed rather than silently turning into a different Type personality.

### Provider / product boundary

```text
Platform
  -> Product / Plan
    -> Connection Profile
      -> Model catalogue identity
      -> Invocation target (model/endpoint/deployment/profile)
      -> code-owned protocol Adapter
        -> provider-neutral HTTP/SSE transport where applicable
```

Commercial products, plans, regions and entitlements therefore do not imply new Python adapter classes. Metadata is allowed to select only installed code-owned transports. Model resolution is live discovery -> saved cache -> profile-declared bundled fallback -> manual ids, with explicit local override as the final authority for user-supplied metadata.

### Adapter consolidation

The four previously duplicated native Chat modules changed from:

- Zhipu 363 lines
- Moonshot 388 lines
- Qwen 374 lines
- Xiaomi 368 lines
- **1,493 total**

to:

- Zhipu 103 lines
- Moonshot 110 lines
- Qwen 102 lines
- Xiaomi 101 lines
- **416 total (72.1% reduction)**

Shared behavior moved to `http_sse.py` and `chat_completions_common.py`; vendor modules retain only verified protocol/request-policy differences. The full `providers/` Python package is 3,879 lines after including the shared infrastructure and product/catalogue model.

## Final regression results

- `pytest -q`: **448 passed, 1 skipped, 42 subtests passed**
- coverage run: **70% total branch-aware coverage**, above the configured 60% gate
- `scripts/release_check.py`: **PASS**, all version surfaces 0.1.0
- `python -m compileall -q src tests scripts`: **PASS**
- `node --check ui/index.mjs`: **PASS**
- wheel build: **PASS** (`kiro_byo-0.1.0-py3-none-any.whl`)
- fresh virtualenv install/import/`kiro-byo --help`: **PASS**
- release-tree mutable/secret-state regression: **PASS**
- optional Ruff/mypy checks were not claimed because those tools are not installed in the continuation runner

## Residual verification boundary

The automated suite does not substitute for authenticated live calls to every external provider/product, because this release tree intentionally ships no user credentials. Likewise, exact compatibility with a future KiroCrew build must still pass Kiro BYO's semantic integration preflight against that installed source. Those are environmental verification boundaries, not reasons to couple commercial product churn back into adapter code.
