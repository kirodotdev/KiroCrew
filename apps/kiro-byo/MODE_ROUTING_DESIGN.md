# Kiro BYO Mode Routing & Execution Planner

Kiro BYO keeps **Mode**, **Type**, and **Model** as separate concepts. The active Mode (for example Tiga) is selected in the Kiro BYO management UI. That Mode exposes its Types directly in KiroCrew's conversation picker beside concrete provider models. Each Type uses a private wire sentinel (`kiro-byo-type:<mode-id>:<type-id>`) only to carry the per-session execution policy; the Mode itself is never represented as a fake model. A concrete provider model selected in Crew bypasses BYO Mode routing completely.

Type ids are local to a Mode, not global product ids. Different Modes may expose completely different Type sets, counts, ordering, names and execution semantics, and may reuse the same local Type id without collision because the wire identity is namespaced by Mode. Core routing and dashboard code must therefore enumerate the active pack instead of knowing Tiga's four Type names.

## Research baseline (2026-10-13)

The implementation borrows principles rather than product-specific routing rules:

- **Kiro** documents Auto as per-task routing to the optimal model with quality/cost balancing: <https://kiro.dev/docs/models/>
- **Amazon Bedrock Intelligent Prompt Routing** constrains a candidate pool, predicts response quality, and uses a fallback/quality-difference threshold to balance quality and cost: <https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-routing.html>
- **Microsoft Foundry Model Router** exposes Balanced / Cost / Quality modes, task-aware routing, custom model subsets, resolved-model/fallback observability and agent capability eligibility: <https://learn.microsoft.com/azure/foundry/openai/concepts/model-router>
- **Alibaba Model Studio Model Routing** uses an explicit candidate set and reports the actual resolved model: <https://www.alibabacloud.com/help/en/model-studio/model-routing>
- **OpenRouter Auto** emphasizes keeping routing current as model quality/usage shifts instead of hard-coding one generation: <https://openrouter.ai/blog/announcements/introducing-the-new-auto-router/>
- **Tencent CMR** separates the decision/classification layer from model execution and supports classifier models such as Jev: <https://cloud.tencent.com/document/product/1829/135328>
- **DeepSeek Harness + dsh-jev** keeps decisions narrow: Jev narrows/ranks/gates/routes while deterministic harness code executes; it supports off/shadow/enforce modes, verified-availability model routing, structured decision logs, bounded state, explicit uncertainty/fallback, and does not let a classifier directly generate or execute side effects: <https://github.com/buberlo/dsh-jev>
- **DSH model-router implementations** reinforce two useful boundaries: never override an explicit manual model choice; keep fallback bounded and replay-safe; project real per-model usage/cost from durable logs rather than inventing synthetic accounting. BYO keeps those principles while using its own backend mode entry as the routing entry point.

## Modes and internal Policy Packs

The router core is pack-neutral. It loads data-only JSON Policy Packs and builds an execution plan from the selected pack/policy. Built-ins ship inside the Python package; user packs live at `<KIRO_BYO_HOME>/policy_packs/*.json` and are re-read on every config/UI load, so adding or editing a pack does not require a restart. Pack data can express routing weights, complexity adaptation, secondary-stage gates/objectives, bounded verifier/reviewer roles, and an abstract reasoning intent. It cannot execute code, call tools, or declare provider/model capabilities.

The default user-facing mode is **Tiga**, implemented by the built-in **Tiga Policy Pack**:

- **Sky Type** — speed/economy objective. Multi-model use is allowed but exceptional: only very high-risk turns justify a low-overhead verifier.
- **Multi Type** — expected-utility objective; default. It dynamically decides whether one model is enough or whether a value-oriented verifier earns its extra latency/cost.
- **Power Type** — capability/reliability objective. Strong independent verification is used proactively for substantive, coding, tool-heavy, or high-reasoning work; trivial work can remain single-pass.
- **Glitter Type** — maximum-quality objective. A strongest-available, bounded tool-free final review is part of the normal plan, preferring an independent model and explicitly falling back to self-review if necessary.

The Tiga names belong to the pack, not to the router core. A future Dyna/Gaia/Cosmos/custom pack can expose a completely different policy set while using the same Execution Planner contract. The management UI calls these top-level choices **Modes** and only displays the Types contained by the selected Mode. Type switching happens in KiroCrew's conversation picker, where the active Mode contributes entries such as `Sky Type`, `Multi Type`, `Power Type`, and `Glitter Type`; real provider models follow as independent choices.

Model reasoning controls remain orthogonal to Policy Packs. A concrete model may be user-locked to Thinking On/Off or to an explicit effort level; only Auto/default delegates that invocation to the Type. Packs express generic Auto policies (for example `prefer_off`, `adaptive`, `highest`, or `adaptive_per_call`) and the capability adapter maps them only onto controls and effort levels the chosen model really supports. Packs cannot invent thinking toggles, `high`/`max`, image support, tool support, or context capacity.

## Decision order

1. Build turn signals from the current prompt, history, image presence, tool pressure and reasoning effort.
2. Apply hard eligibility gates for explicitly unsupported image/tool/context requirements.
3. Score eligible models under the selected policy.
4. Prefer exact support for requested reasoning effort.
5. Record the ordered candidate list, scores, signals and resolved model in Runtime Trace.
6. If the first request fails before any model output/tool state exists, try the next eligible candidate. After model/tool state exists, fail closed rather than switch providers mid-turn.
7. **Execution Planner applies the selected Type.** A Type is an optimization policy, not a model-count rule. Sky/Multi/Power may remain single-pass or add a verifier according to their own benefit threshold; Glitter normally adds a reviewer.
8. **Long productive execution is not bounded by a small fixed round count.** The execution guard watches repeated identical tool outcomes and short action cycles, requests one re-plan on stagnation, and stops only if the loop continues; a high emergency backstop protects against catastrophic runaway.
9. **Secondary stages are tool-free.** They inspect the accepted turn transcript and draft, return only the final answer, and can never execute side effects. If a secondary stage fails, is interrupted, returns empty output, or unexpectedly requests a tool, BYO keeps the primary draft.
10. Runtime Trace records the primary route, per-invocation reasoning decisions, review target/latency, execution-guard interventions, whether the answer changed, and any review failure/fallback.

Models may optionally declare `routing.quality`, `routing.speed` and `routing.economy` values in provider metadata (0.0–1.0). When absent, the router starts from neutral capability/tier priors; no provider brand is special-cased.
