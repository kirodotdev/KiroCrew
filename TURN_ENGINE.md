# Kiro BYO 0.1.3 Turn Engine

The live agent loop now advances through the provider-neutral `TurnState` contract.
Provider transports, Tiga policy names and concrete tool implementations are not
part of the phase vocabulary.

```text
PREPARE
   │
   ▼
 ROUTE
   │
   ▼
 INVOKE ───── pristine first-round failure ───► ROUTE
   │
   ├── streamed content ─► STREAM
   │                         │
   └─────────────────────────┤
                             ▼
                           TOOL
                             │
                             ▼
                         CONTINUE
                             │
                             └──────────────► INVOKE

final candidate ────────────────────────────► POST_STAGE
                                                │
                                                ▼
                                             COMPLETE
```

The phase state machine decides lifecycle only. Concrete work is delegated through narrow capability seams rather than provider/product-specific branches:

- `ModelRouter`: route the primary model and collaboration workers
- `ProviderInvoker`: execute one provider/model round
- `ToolExecutor`: execute native/MCP/deferred-search batches
- `SessionProjection`: reconstruct runtime state from canonical session events
- `PostStageRunner`: detached verifier/reviewer stage
- `ReasoningResolver`: map generic policy intent onto one concrete model invocation

Default implementations adapt the existing Kiro BYO modules. This is deliberately not an everything-is-a-plugin framework: the goal is a small replaceable boundary around the Turn Engine.

`RoundOutcome` is the small control-facing summary used to distinguish final vs
tool-producing provider rounds. Provider-specific response/context objects stay
behind the provider seam.

`turn.phase` Runtime Trace entries expose the actual live transition sequence for
diagnostics and characterization tests. They are diagnostic events, not a second
persistent state machine.

## Policy compilation boundary

The user-facing Policy Pack is split before execution:

```text
Policy Pack
   │
   ├── semantics ─────────────► UI / on-demand assistant explanation
   │
   └── execution
          │
          ▼
   ExecutionIntent
          │
          ▼
      Turn Engine
```

`ExecutionIntent` contains routing, reasoning, secondary-stage, sticky and collaboration facts but no Mode/Type display names. Relay and review system prompts are generic execution-role prompts, so normal model turns are not polluted with Tiga product terminology.
