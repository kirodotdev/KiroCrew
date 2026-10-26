# Kiro BYO Policy Packs

Policy Packs are the internal hot-plug contract behind Kiro BYO user-facing Modes. They keep the router generic without exposing implementation jargon in the UI.

The core planner knows only a generic schema. The built-in **Tiga Policy Pack** currently provides:

- Sky Type
- Multi Type (default)
- Power Type
- Glitter Type

A future Dyna, Gaia, Cosmos, research, coding, local-model, or user-defined pack can expose a different set of Types without changing `mode_router.py`.

## Loading and hot reload

Built-in packs ship as JSON package data under `kiro_byo/policy_packs/`.

User packs are loaded from:

```text
<KIRO_BYO_HOME>/policy_packs/*.json
```

The directory is re-read on every runtime config load and management-UI status refresh. Adding, editing, or removing a JSON pack therefore does not require restarting Kiro BYO. If a user pack uses the same `id` as a built-in pack, the user pack intentionally overrides it. Invalid files are ignored by the catalog, surfaced as load errors in the management UI, and cannot execute Python code.

If the currently selected pack is removed or becomes invalid, runtime config validation fails closed instead of silently switching execution policy.

## Selection contract

`providers.json` stores only the active selection:

```json
{
  "mode": {
    "id": "tiga",
    "type": "multi"
  }
}
```

The Kiro BYO management UI selects the active **Mode** (the internal Policy Pack) and displays the Types that Mode provides, but it does not switch Types. KiroCrew's conversation picker receives private Type sentinels (`kiro-byo-type:<mode-id>:<type-id>`) plus the real provider model ids. Selecting a Type is an explicit **user-owned** action and invokes the Policy Pack planner for that session; selecting a concrete model bypasses Mode routing entirely. The assistant/router may select models inside the active Type but cannot switch, auto-switch, or claim to have switched the Type.

## Schema v3: semantics + execution + adaptive routing policy

0.1.3 split each Type into user-facing semantics and machine execution data. 0.1.7 keeps that boundary and promotes the built-in Tiga pack to schema v3 so adaptive-routing behaviour is data too: runtime blending, capability-fit biases, capability fallback scoring, reviewer scoring and Glitter worker routing are declared in the Policy Pack rather than hidden as Type-specific Python constants.

```json
{
  "schema_version": 3,
  "id": "example",
  "display_name": "Example Policy Pack",
  "short_name": "Example",
  "description": "Example execution policies",
  "default_policy": "balanced",
  "policies": [
    {
      "id": "balanced",
      "semantics": {
        "display_name": "Balanced",
        "localized_name": "",
        "description": "Balanced quality, speed, and economy",
        "short_description": "Balanced default"
      },
      "execution": {
        "reasoning_intent": "inherit",
        "routing": {
          "quality": 0.5,
          "speed": 0.25,
          "economy": 0.25,
          "complexity_shift": {
            "mode": "adaptive",
            "strength": 0.25,
            "center": 0.5,
            "speed_share": 0.55,
            "economy_share": 0.45
          },
          "adaptive": {
            "history_limit": 500,
            "half_life_days": 3,
            "speed_alpha_base": 0.22,
            "speed_alpha_speed_weight": 0.85,
            "reliability_base": 0.12,
            "reliability_quality_weight": 0.26
          },
          "fit": {
            "image_unknown_penalty": 0.08,
            "tool_unknown_penalty": 0.05
          }
        },
        "reasoning": {
          "thinking_auto": "adaptive",
          "effort_auto": "adaptive"
        },
        "secondary": {
          "role": "verify",
          "objective": "value",
          "trigger": {"complexity_gte": 0.92},
          "reason": "expected gain justifies verification",
          "self_reason": "single eligible model; self verification",
          "rationale": "value verifier enabled",
          "single_mode": "single_pass",
          "multi_mode": "verified",
          "reasoning_intent": "balanced",
          "scoring": {
            "quality": 0.47,
            "speed": 0.21,
            "economy": 0.20,
            "reliability": 0.12
          }
        }
      }
    }
  ]
}
```

`semantics` is consumed by KiroCrew UI and by the assistant only when the user explicitly asks about Mode/Type meaning. `execution` is compiled into a name-free `ExecutionIntent` for routing and the Turn Engine. The Turn Engine therefore never branches on names such as Sky/Multi/Power/Glitter.

Schema v1/v2 custom packs remain supported and receive compatibility defaults at load time, so existing user packs do not break. New packs should use schema v3 when they want routing behaviour to be fully data-driven.

### Routing

`quality`, `speed`, and `economy` are non-negative weights in the range 0–1. They are normalized by the router.

`complexity_shift` is optional:

- `none`: no task-complexity adjustment.
- `adaptive`: shifts weight toward quality as complexity rises; `strength` controls the amount.
- `threshold`: adds `quality_add` after `threshold` is crossed while reducing speed/economy proportionally.

Hard model capability gates always run before scoring. A pack cannot override explicit image/tool/context incompatibility.

`adaptive` controls how real recent runtime evidence is shaped and blended (history window, recency half-life, sample/confidence thresholds, latency evidence, speed blend and reliability contribution). `fit` contains bounded tie-breakers for known/unknown capability and reasoning alignment. These are internal policy facts, not user-editable UI controls.

`fallback_scoring`, `secondary.scoring`, `collaboration_routing`, and `collaboration_provider_affinity` let a v3 pack describe capability fallbacks, verifier/reviewer objectives, and relay-worker routing without teaching the Router Core any Type name or magic score formula.

### Secondary stage

`secondary` is optional. When present, it describes a bounded, tool-free verifier/reviewer stage after the normal agent/tool loop.

`role`:

- `verify`
- `review`

`objective` controls how the secondary model is chosen:

- `economy`
- `value`
- `quality`

A trigger can use any combination of:

- `always`
- `complexity_gte`
- `code`
- `tools`
- `code_and_tools`
- `reasoning_any`

Non-`always` trigger conditions are ORed. `code_and_tools` is itself an AND condition.

Secondary stages never receive tool access. Failure, interruption, an empty answer, or an unexpected tool call preserves the accepted primary result.

### Auto reasoning policy is not a capability declaration

A Type may declare independent Auto policies:

```json
"reasoning": {
  "thinking_auto": "adaptive",
  "effort_auto": "adaptive"
}
```

`thinking_auto` supports `inherit`, `prefer_off`, `adaptive`, `prefer_on`, and
`adaptive_per_call`. `effort_auto` supports `inherit`, `lowest`, `adaptive`, `highest`,
and `adaptive_per_call`. These values are policy intent only.

The concrete model and explicit user settings remain authoritative:

- user `Thinking=On/Off` is a hard lock; only `Auto` delegates to the Type;
- fixed-on/fixed-off/unknown thinking capability cannot be overridden by a Type;
- explicit user effort is a hard lock; only Auto/default delegates to the Type;
- a model with `low/medium/high` cannot be given `xhigh` or `max`;
- a model with no effort control receives no synthetic effort selector;
- when effective thinking is Off, no reasoning-effort value is sent.

The legacy `reasoning_intent` remains an abstract compatibility hint for secondary stages.
Policy Packs therefore remain orthogonal to provider discovery and model capability ownership.

### Tier and collaboration boundaries

`routing.tier_pool` selects the normal model-role pool (`any`, `fast`, `balanced`, or `strong`). Model roles are many-to-many, so a concrete model may belong to more than one pool; a Type may also invoke several models from its allowed pools during one task. `routing.primary_tier_pool` can make the leader stricter than the general review pool. `routing.tier_fallback` is either `capability` (an out-of-tier model is allowed only when the selected tier has no capable model) or `strict` (fail closed instead of silently changing the Type's character). Relay policies may additionally declare `collaboration_tier_pool`; workers are re-scored from each current subtask rather than inheriting the leader candidate order or being locked to the leader's provider.
