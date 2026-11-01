# Dynamic Model Catalogue & Invocation Target Architecture

This document records the provider/model catalogue design used by Kiro BYO
0.1.3 after the 2026-10 provider and end-to-end audits. The goal is to survive fast-moving
platform/product/model changes without turning every commercial plan or model
release into another Python provider adapter.

## External platform patterns used as the design baseline

The design deliberately follows patterns already used by large domestic and
international AI platforms rather than assuming every provider is a flat
`base_url + model_id` service.

- **Alibaba Cloud Model Studio / Bailian** exposes a management-plane model list
  (`GET /api/v1/models`) with filters for provider, modality, model capability
  and deployment mode. Catalogue rows can carry context/output limits,
  capabilities/features and lifecycle/offline information. The management
  endpoint itself varies by region/workspace.
  https://help.aliyun.com/en/model-studio/list-models
- **Volcano Engine Ark** treats public models and subscription plans as separate
  control-plane concepts. `models` manages public foundation models while
  `plans` manages Agent Plan / Coding Plan, and Ark exposes an API specifically
  for querying the models supported by an Agent Plan. A plan therefore changes
  entitlement/connection data; it is not a new inference protocol.
  https://docs.volcengine.com/docs/ark/models-and-plans?lang=zh
  https://docs.volcengine.com/docs/ark/list-ark-agent-plan-model-api?lang=en
- **Amazon Bedrock** separately discovers foundation models and inference
  profiles (`ListFoundationModels` vs `ListInferenceProfiles`). The resource
  used for inference is therefore not necessarily the catalogue model id.
  https://docs.aws.amazon.com/us_en/bedrock/latest/userguide/models-get-info.html
- **Microsoft Foundry** separates model catalogue, deployment option/type and
  deployment resource. A deployment name is the value the application passes
  in the inference `model` field, so stable model identity and wire invocation
  target are distinct concepts.
  https://learn.microsoft.com/en-us/azure/foundry/concepts/deployments-overview
  https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/endpoints
- **Google Gemini** publishes explicit model deprecation/shutdown lifecycle
  information. Model availability is time-varying state rather than a permanent
  hard-coded enum.
  https://ai.google.dev/gemini-api/docs/deprecations/
- **Xiaomi MiMo** exposes pay-as-you-go and Token Plan as different commercial
  access paths and keys while retaining compatible inference protocols. Token
  Plan model membership is broader than two language models and changes over
  time; MiMo also publishes model retirement dates. Kiro BYO therefore treats
  bundled plan model ids only as an outage fallback, never as an allowlist.
  https://mimo.mi.com/docs/en-US/quick-start/faq/token-plan/Plans%26Pricing
  https://mimo.mi.com/docs/en-US/tokenplan/Token%20Plan/quick-access
  https://mimo.mi.com/docs/quick-start/model

## Kiro BYO identity layers

```text
Platform
  └─ Product / Plan
       └─ Connection Profile
            ├─ Model Catalogue Identity
            ├─ Invocation Target
            └─ Protocol Adapter
```

- **Platform**: vendor/service family, e.g. Xiaomi MiMo or Alibaba Model Studio.
- **Product / Plan**: commercial/API access path, e.g. pay-as-you-go API, Token
  Plan, Coding Plan, Agent Plan.
- **Connection Profile**: volatile region/base URL/auth/entitlement/discovery
  metadata.
- **Model catalogue identity**: stable model identity shown to routing/UI and
  used for saved model preferences.
- **Invocation target**: identifier actually sent to inference. For direct APIs
  it normally equals the model id; managed services may instead use endpoint,
  deployment or inference-profile ids.
- **Protocol adapter**: executable wire semantics. Products/plans do not create
  adapters unless their protocol actually differs.

## Catalogue resolution order

For a configured provider Kiro BYO uses this operational order:

```text
live provider/control-plane discovery
       ↓ unavailable
last successful saved catalogue (cache)
       ↓ unavailable and profile permits it
bundled entitlement fallback snapshot
       ↓ unavailable
manual user model ids
```

A bundled list is never a generation allowlist. New chat/reasoning models found
by the provider are accepted without waiting for a Kiro BYO release. Obvious
ASR/TTS/embedding/rerank/image/video-only products are excluded by capability,
modality or a conservative **deny** heuristic; unknown future model names remain
eligible.

## Capability resolution

Capability facts have three layers:

1. **Provider discovery** — current control-plane facts win when explicitly
   supplied (context size, modalities, function calling, lifecycle, target).
2. **Curated metadata** — fills facts the provider catalogue omitted; it must not
   mask an explicit newer live value.
3. **Local operator override** — explicit final authority for a just-launched
   model/private deployment before Kiro BYO itself is updated.

Missing capability metadata remains **unknown**, not `false`. This is important
for fast-moving providers: a newly launched model must not be rejected merely
because the local release has not learned its feature matrix yet.

## Routing metadata is separate and many-to-many

Capability answers whether a model *can* execute a turn. Routing metadata answers
which optimization roles it is good at. The normalized optional shape is:

```json
{
  "routing": {
    "quality": 0.94,
    "speed": 0.70,
    "economy": 0.72,
    "tiers": ["strong", "balanced"]
  }
}
```

`tier` is not a one-model assignment and `tiers` is intentionally plural. A
near-flagship value model can serve both `strong` and `balanced`; a flagship
low-latency endpoint can serve both `strong` and `fast`. Tiga Types consume
these pools and may use several models in one task (primary, verifier, worker,
reviewer). Live provider routing metadata wins when explicitly supplied, bundled
curated routing fills omissions, and `model_catalog.override.json` is the final
operator authority. Generic name inference exists only for old/custom catalogues
that have none of those sources.

Optional operator metadata lives in:

- `$KIRO_BYO_HOME/config/model_catalog.override.json`
- `$KIRO_BYO_HOME/config/provider_products.override.json`

Both files are data only. They can describe new models, plans, regions and
endpoints while binding to an already-installed protocol adapter; they cannot
create executable adapter code.

## Lifecycle

Normalized lifecycle values include `active`, `preview`, `deprecated`,
`retiring`, and `shutdown`. Shutdown/retired discovery rows are excluded from
new selection. Retiring/deprecated rows remain representable so an existing
configuration can be migrated deliberately rather than disappearing without
context.

As of 2026-10-14, Xiaomi documents MiMo-V2.5-Pro and MiMo-V2.5 as retiring on
2026-10-21. Token Plan's bundled agent fallback therefore includes those
currently entitled text-agent models as `retiring`, while live discovery always
wins and will naturally remove them when the platform does.

## Regression scenarios

`tests/test_dynamic_model_catalog.py` protects the architecture against:

- paginated rich Model Studio-style discovery;
- capability/modality parsing and non-agent filtering;
- newly launched unknown chat models;
- separate model identity vs endpoint/deployment/inference-profile target;
- a newly added plan/region through data only, without an adapter class;
- local capability metadata for a new model;
- provider-catalogue outage with saved-cache fallback;
- model shutdown/retirement lifecycle changes;
- stale curated metadata overriding newer live provider facts.

## Routing metadata provenance (0.1.3)

Routing hints are optimization metadata, not model capabilities. 0.1.3 records
field-level provenance for `quality`, `speed`, `economy`, and `tiers`, because a
single model row can legitimately mix sources. For example, live discovery may
publish latency while verified curated metadata fills quality and routing roles.

Stable authority order:

```text
local operator override
        >
live provider discovery
        >
verified curated profile
        >
historical outage cache
        >
generic model-name inference
```

The first four sources are persisted as routing provenance with a confidence
label. Name inference is never promoted to catalogue truth: it remains a
low-confidence, last-resort Router prior only when no explicit routing roles or
scores exist. A cached catalogue is deliberately demoted below current curated
routing facts during merge, so an outage does not make stale live metadata more
authoritative than a newer verified bundled profile.
