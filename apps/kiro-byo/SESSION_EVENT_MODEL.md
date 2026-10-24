# Kiro BYO 0.1.3 Session Event Model

Kiro BYO 0.1.3 continues the event-sourced session work introduced in 0.1.2 and
removes two remaining duplicate durable state paths. The append-only typed event
stream is now the durable source for the provider transcript, provider continuation
metadata and sticky routing pins. Live `Session.messages` and `Session.route_pins`
remain runtime projections for compatibility and fast access.

The harness rule is simple: record semantic state changes once, derive runtime
views from those events, and keep bounded diagnostics outside the durable truth.

## Canonical events

Every event has a monotonic `seq`, epoch `time`, `type`, and object `data` payload.
Core event classes include:

- `session/start`, `session/resume`
- `turn/start`, `turn/steer`, `turn/end`
- `run/start`, `run/retry`, `run/resume`, `run/replan`, `run/recovery_rewind`, `run/completed`, `run/failed`, `run/cancelled`
- `model/selected`, `model/invoke`, `model/result`, `model/fallback`
- `user/message`, `assistant/message`, `assistant/attempt`, `system/message`
- `provider/context`
- `routing/pin`, `routing/unpin`, `routing/clear`
- `tool/call`, `tool/result`
- `review/start`, `review/end`, `review/skipped`
- `agent/plan`, `agent/subagents_started`, `agent/started`, `agent/completed`, `agent/failed`, `agent/cancelled`, `agent/skipped`
- `assistant/revision`
- `transcript/truncate`, `transcript/reset`, `transcript/clear`
- `compaction/summary`

Provider-private continuation blobs no longer need to live inside the canonical
assistant-message payload. A `provider/context` event targets the corresponding
message index; the message projection re-attaches it only for the exact
provider/model replay path that already passed provenance checks.

## Runtime projections

`derive_session_projection(events)` reconstructs the active runtime state:

```text
Session Event Log
      │
      ├── TranscriptProjection  -> Session.messages
      │       └── provider/context is re-attached here
      │
      └── RoutingProjection     -> Session.route_pins
```

Rollback, review replacement, compaction and pin changes never rewrite prior
events. They append a semantic event whose projection produces the current state.

## Persistence generations

- schema v1: legacy `messages` snapshot
- schema v2 (0.1.2): canonical events plus a separate durable `route_pins` record
- schema v3 (0.1.3): canonical events only for transcript/provider
  continuation/routing pins; the runtime projections are rebuilt on load

A schema-v2 record is migrated in memory by translating its side `route_pins`
record into `routing/pin` events. The next save writes schema v3 and omits the
separate routing state. Schema-v1 message snapshots still bootstrap into events.

The session-event vocabulary is currently schema version 2.

## Compatibility boundary

Old tests/integrations may still mutate `Session.messages` or `Session.route_pins`
directly. Runtime-owned code uses event helpers. Persistence performs a final
projection check and converts any direct mutation into explicit `transcript/reset`
or routing events rather than silently losing it or writing a second source of
truth.

This seam is compatibility-only: new runtime code should mutate projections through
session-event helpers.

## Runtime Trace is not session truth

Runtime Inspector remains a separate bounded diagnostic stream. Provider stream
chunks, latency measurements and transport debugging belong there and may be
discarded. Semantic model/tool/routing/session state belongs in the event stream.
Runtime trace entries can carry the nearest session event sequence for correlation
without becoming required for replay or recovery.

## Agent task and durable Run boundary

Agent lifecycle events are semantic observability for the current execution plan, but a child transcript/mailbox is still not a durable session projection. A child `task_id` is stable while its loaded BYO runtime exists and can be continued, messaged or cancelled; a process restart restores only the bounded child metadata projection and marks stale child execution paused rather than fabricating a private transcript.

Long-turn durability lives one layer above child transcript state in the `Run -> Step -> Attempt -> Checkpoint` ledger. The Run stores control-plane identities and canonical event-sequence anchors. Clean/read-only interrupted Runs can be explicitly retried/resumed/replanned by reconstructing the original user input from the event log and rewinding the projected transcript to the recorded pre-turn boundary. Type replan clears the stale routing pin before fresh routing; concrete-model recovery preserves the pin.

Mutating tool admission is checkpointed before execution. If restart occurs across that boundary without a trustworthy completion record, the Run becomes `unknown_side_effect` and automatic replay is rejected. This preserves one durable source of conversational truth while still permitting safe long-turn recovery; it deliberately does not claim that process-local child transcripts themselves survive a restart.
