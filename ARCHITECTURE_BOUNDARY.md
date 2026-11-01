# Kiro BYO / KiroCrew Architecture Boundary

Status: **normative design boundary for Kiro BYO 0.1.0+**.

Kiro is a benchmark for mature harness design, not a checklist to clone. KiroCrew
already owns product-level orchestration. Kiro BYO should implement only the
capabilities required to be a trustworthy provider-extensible Agent Harness and
should integrate with Crew-owned behavior instead of duplicating it.

## Ownership map

```text
                    KiroCrew
                       │
                 Agent Backend
                       │
          ┌────────────┴────────────┐
          │                         │
       Kiro CLI                  Kiro BYO
      official backend          third-party backend
          │                         │
     official models           Provider Layer
                            MiMo / DeepSeek /
                            Qwen / Claude /
                            Gemini / Kimi / BYO...
```

KiroCrew decides **which Agent Backend** runs a session. Kiro BYO, once selected,
decides **which Provider / Model** serves that backend. Kiro BYO is therefore a
sibling of the official Kiro CLI at the Agent Backend boundary, not a child of
Kiro CLI and not a KAS wrapper. Product/orchestration/policy ownership remains in
KiroCrew; provider/model ownership remains inside Kiro BYO.

Within Kiro BYO, provider ownership is itself layered:

```text
Platform → Product → Connection Profile → adapter_transport → protocol module
```

Platform/Product/Profile describe vendor, commercial entry, billing/region and
connection metadata. They are data. Only `adapter_transport` selects executable
wire-protocol code. Adding a subscription plan, region or entitlement must not
create another adapter class. See `PROVIDER_PRODUCT_MODEL.md`.

## Crew-owned: do not duplicate

Kiro BYO does **not** build a second product/orchestration layer merely because
an official Kiro capability has the same name.

- Agent/member selection UI
- Multi-agent/Crew orchestration and scheduling
- Side Chat policy and UX
- Saved-agent governance and editor UX
- User-facing workflow editor
- Dashboard/navigation/product surfaces
- Crew-owned lifecycle/event UI

If one of these needs a Kiro BYO fact, expose that fact through a narrow contract;
do not create a second scheduler or product configuration system.

## Kiro BYO-owned: must be reliable in the harness

These are execution/runtime responsibilities Crew cannot substitute after it has
selected Kiro BYO as the backend.

- ACP session lifecycle and durable session restoration
- Agent loop and provider communication
- Streaming, cancellation and steering
- Provider abstraction and open provider support
- Native file/web/shell execution
- Long-running process ownership and process-tree cleanup
- MCP client/transports and protocol lifecycle
- Code intelligence used by the running coding agent (parser/AST, then LSP)
- Harness context/runtime state
- Typed append-only backend session event log and message projection
- Local hard safety boundaries for Kiro BYO-owned execution
- Telemetry needed to explain Kiro BYO runtime behavior

### Session state and diagnostics

Kiro BYO's durable backend session record is a typed append-only event stream.
Provider-facing messages are derived from that record; Runtime Trace and turn
telemetry are diagnostic projections only and must never become an alternate
source of truth. KiroCrew still owns the host/session UX and chooses when the
backend session is created, resumed, closed or deleted.

## Shared boundary: one authority, explicit bridge

These features may span both systems. Every implementation must name which side
owns the decision and which side only projects/transports it.

### Permissions

Crew owns user-facing permission policy and the permission decision. Kiro BYO owns
the executable dispatcher for its native/MCP tools and **must route each privileged
execution through Crew's ACP permission round-trip** before execution. Local Kiro
API restrictions may only make the result stricter; they never bypass Crew.

### Saved-agent tools / Skills

Crew owns saved-agent governance. Kiro BYO may consume the projected runtime view
(prompt/tool visibility) needed to execute the selected agent. It must not claim
Crew's stronger "full saved-member spec" capability unless the complete contract
is implemented and evidenced.

### Hooks

Kiro BYO may have internal lifecycle callbacks (`before_tool_execute`, provider
telemetry, cleanup) as implementation architecture. A second user-programmable
Hooks product belongs out of scope while Crew already owns that surface.

### Subagents

Crew remains the orchestration authority. Native subagents inside Kiro BYO are out
of scope unless a future use case cannot be expressed through Crew and the design
explicitly assigns session ownership, cancellation, permissions, token accounting
and recovery. Avoid nested schedulers by default.

### Knowledge / context

Kiro BYO may own execution-time context storage/retrieval needed by the harness.
Crew owns product-level knowledge/workspace UX. Exchange typed context facts rather
than duplicating user-facing knowledge stores.

## Capability declaration rule

A Crew capability is **positive opt-in by evidence**:

```text
implementation
    ↓
behavioral test
    ↓
Crew semantic definition checked
    ↓
capability membership
```

Never infer a capability because another backend has it, because an adjacent
feature "looks similar", or because it raises a parity score.

## Decision filter for official Kiro features

Before implementing an official Kiro feature, ask:

1. Is this intrinsic to running the Harness correctly?
2. Can Crew already provide the product/orchestration part?
3. If Crew owns it, what is the smallest runtime contract Kiro BYO needs?
4. Would implementing it create a second scheduler/policy/UI authority?
5. What behavioral evidence would justify advertising it?

Official Kiro is therefore a **design benchmark**, not the Kiro BYO TODO list.

## Explicit current out-of-scope duplication

Until this document is deliberately revised, do not add these solely for parity:

- second multi-agent scheduler
- second saved-agent manager/editor
- second Side Chat implementation
- second workflow product
- second user-facing hooks system
- Kiro-specific IDE/Web/Mobile surfaces

## Current high-leverage Kiro BYO priorities

```text
trustworthy 0.9 integration / release QA
            ↓
long-running process model        ← 0.9.13 implementation
            ↓
Tree-sitter parser/symbol layer   ← 0.9.13 adapter + honest fallback
            ↓
LSP                               ← next semantic-intelligence layer
            ↓
MCP lifecycle + stronger capability policy bridge
            ↓
context / provider routing / search-provider improvements
```
## Enforced provider boundary

Kiro BYO follows one direction of adaptation: provider-specific wire protocols adapt to the Kiro BYO harness; the harness does not branch on provider brands. `acp_server.py` and `turn_runner.py` may import the provider facade (`kiro_byo.providers`) but must not import concrete vendor adapters or compare provider identity to vendor names. Vendor request/response quirks, native reasoning controls, continuation state, cache semantics and tool-call wire formats stay under `src/kiro_byo/providers/` (with catalog/config metadata outside the execution loop).

`tests/test_provider_boundary.py` is a regression gate for this rule. This deliberately protects the Agent Harness from accumulating DeepSeek/Anthropic/Gemini/Qwen/etc. conditionals as provider support grows.



## 0.1.3 narrow capability seams

The Turn Engine consumes six narrow provider-neutral capabilities: `ModelRouter`, `ProviderInvoker`, `ToolExecutor`, `SessionProjection`, `PostStageRunner`, and `ReasoningResolver`. These are internal replacement seams, not a second plugin product. Provider brands remain below the provider facade; Mode/Type user semantics remain above the compiled `ExecutionIntent`.

## 0.1.3 core dependency direction

Kiro BYO 0.1.3 makes the Turn Engine dependency direction explicit. The core
executes provider-neutral phases and consumes narrow capability seams; it must
not grow provider-brand branches, product Type names, or a second plugin
framework.

```text
KiroCrew / ACP
      |
      v
  Turn Engine
      |
      +--> RuntimeSeams
      |      +--> ModelRouter
      |      +--> ProviderInvoker
      |      +--> ToolExecutor
      |      +--> SessionProjection
      |      +--> PostStageRunner
      |      +--> ReasoningResolver
      |
      +--> TurnState / RoundOutcome

Policy Pack --> ExecutionIntent --> ModelRouter
Session Events --> SessionProjection --> Turn Engine
Provider modules ---------------------> Provider facade
```

Dependency rules are regression-tested in `tests/test_core_dependency_boundary.py`:

- `turn_state`, `execution_intent`, and `session_events` stay leaf contracts and
  do not import the Agent runtime or provider implementation layer;
- `mode_router` remains model-intelligence code and does not import tool/provider
  execution modules;
- provider protocol modules do not import Agent/Policy runtime code;
- `capability_seams` remains a small adapter surface rather than becoming an
  everything-is-a-plugin framework;
- `ExecutionIntent` contains no built-in Tiga Type names.
