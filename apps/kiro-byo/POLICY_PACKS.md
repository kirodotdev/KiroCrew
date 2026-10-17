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

The Kiro BYO management UI selects the active **Mode** (the internal Policy Pack) and displays the Types that Mode provides, but it does not switch Types. KiroCrew's conversation picker receives private Type sentinels (`kiro-byo-type:<mode-id>:<type-id>`) plus the real provider model ids. Selecting a Type invokes the Policy Pack planner for that session; selecting a concrete model bypasses Mode routing entirely.

## Schema v1

Minimal example:

```json
{
  "schema_version": 1,
  "id": "example",
  "display_name": "Example Policy Pack",
  "short_name": "Example",
  "description": "Example execution policies",
  "default_policy": "balanced",
  "policies": [
    {
      "id": "balanced",
      "display_name": "Balanced",
      "description": "Balanced quality, speed, and economy",
      "routing": {
        "quality": 0.5,
        "speed": 0.25,
        "economy": 0.25,
        "complexity_shift": {
          "mode": "adaptive",
          "strength": 0.25
        }
      },
      "secondary": {
        "role": "verify",
        "objective": "value",
        "trigger": {
          "complexity_gte": 0.7,
          "code_and_tools": true,
          "reasoning_any": ["xhigh", "max"]
        },
        "reason": "expected gain justifies verification",
        "self_reason": "single eligible model; self verification",
        "rationale": "value verifier enabled",
        "single_mode": "balanced_single_pass",
        "multi_mode": "balanced_verified",
        "reasoning_intent": "balanced"
      }
    }
  ]
}
```

### Routing

`quality`, `speed`, and `economy` are non-negative weights in the range 0–1. They are normalized by the router.

`complexity_shift` is optional:

- `none`: no task-complexity adjustment.
- `adaptive`: shifts weight toward quality as complexity rises; `strength` controls the amount.
- `threshold`: adds `quality_add` after `threshold` is crossed while reducing speed/economy proportionally.

Hard model capability gates always run before scoring. A pack cannot override explicit image/tool/context incompatibility.

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
