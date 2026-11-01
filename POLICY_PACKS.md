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

## S4-01 Mode Registry（兼容性迁移说明）

从 S4-01 起，`kiro_byo.mode_registry.load_mode_registry()` 提供只读 Mode
插件注册表；一个 `ModePlugin` 对应一个 Policy Pack，Type 列表作为模式内部结构。
原先的 `load_policy_packs()`、`get_policy_pack()`、`resolve_policy()`、
`default_pack()` 保持原接口与响应语义，并由注册表实现查找与执行准入。

- 策略仍为**数据插件**（JSON），不动态 import 自定义 Python 代码。
- 运行时按需读取，保持已有自定义策略热加载；旧 ID 和配置文件不需要迁移。
- `dyna` 注册为不可执行模式，其 Type 无论通过 UI、API 还是注册表解析都不能执行。
- 注册表插件 API 版本为 `1`，UI 查询响应含 `mode_registry_api_version`
  与 `mode_plugin_kind`（添加字段，向后兼容）。
- 下一阶段可为生命周期、迁移和隔离设计独立契约，但不能以牺牲
  Crew 原生兼容性或 Tiga 四种策略为代价。

## S4-02 — Data-only Mode 生命周期与版本契约

在 **S4-01 的 Mode Registry API v1** 上，S4-02 添加可选的 JSON 元数据：

```json
{"schema_version":4, "id":"example", "plugin_version":"1.2.0", "registry_api":1,
 "enabled":true, "default_policy":"basic", "policies":["... 原有策略对象 ..."]}
```

`plugin_version` 只接受三段数字版本，旧 Policy Pack 未声明时按 `0.0.0`
兼容读取；`registry_api` 未声明时默认 v1，不兼容版本加载前直接拒绝。
这两个字段不参与 Tiga 选模评分、动态路由或模型能力判断。

生命周期管理函数位于 `kiro_byo.mode_lifecycle`：

- `install_or_update_mode(raw, expected_sha256=None)`：在用户数据目录安装或升级**自定义** Mode；新版本号必须递增。建议传入先前状态的 SHA-256 实现并发修改检测。
- `set_custom_mode_enabled(mode_id, enabled, expected_sha256=None)`：显式启停；禁用后注册表拒绝执行 Type。
- `rollback_custom_mode(mode_id)`：在当前内容与最近一次已提交哈希匹配时，回滚上一版本；首次安装的回滚为删除该自定义 Mode。
- `mode_lifecycle_status(mode_id)`：检查 ID、启用状态、版本、内容哈希与回滚冲突状态，不返回秘密信息。

生命周期操作采用跨进程文件锁与同目录原子替换，在操作前检查 schema、Mode ID
和版本，备份上一次配置到自有 `.history` 目录。手工编辑后若内容哈希改变，
拒绝自动回滚，以免覆盖用户修改；中断写入时保留旧活动配置。

**安全边界**：只允许管理自定义 JSON Mode；`tiga` 和 `dyna` 不接受生命周期
修改，Dyna 无法通过伪造版本解锁；不执行第三方代码、不修改 Crew、
不创建新 Provider/工具权限。此阶段不提供安装市场、外部插件执行器或管理 UI。
手动修改 JSON 的旧热加载方式继续有效，但不会自动产生版本化回滚快照。

本阶段提供**一次最近变更的回滚**，不保证跨进程会话中已生效的策略被追溯
替换。已经开始的执行保留其当前策略快照；后续执行读取新的注册表内容。
如发生断电、跨进程非合作修改或外部工具副作用，仍需人工核查。

## S4-03 内置模式插件边界

Tiga、Dyna 已通过 `kiro_byo.modes` 显式可信注册，而不是简单扫描内置目录来发现。内置声明指定固定 Mode/Type 身份、默认 Type 与启用状态，真实执行策略仍从 `src/kiro_byo/policy_packs/{tiga,dyna}.json` 数据资源读取，Model Routing 与工具/权限由 BYO Core 提供。自定义 Mode 仍为 JSON Policy Pack，不导入任意 Python 代码；现有 `load_policy_packs()` / `resolve_policy()` 等调用继续受支持。Dyna 只读预置、不能执行。受控 Mode 生命周期 API 不允许修改 Tiga/Dyna。历史手工 Tiga 同名覆盖保留兼容，若希望获得不可覆盖的内置策略锁定，应在后续版本提供显式迁移路径，不得静默改变现有配置。
