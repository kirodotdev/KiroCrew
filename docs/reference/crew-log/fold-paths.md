# Crew-log fold paths, derived from the fold code

Every path each ADVERTISED session fold renders, with the type its empty
state carries. Produced by calling each fold's own `start()` and `render()`
(`kiro_crew.crew_log.projection._FOLDS`), so it is the shape the routes
`/api/sessions/{slot}/crew-log/projection/{name}` actually serve rather than a
reading of them.

A `null` type is a field whose EMPTY value is null and whose populated value is a
number or a string: the fold declares the key and withholds a measurement nobody
reported. That distinction is the one a reader of these paths must keep -- absent
is not zero, and a fold reports how many turns reported a measurement
(`usage.turns.credits_reported`, `usage.turns.tokens_reported`,
`usage.turns.duration_reported`, `usage.credits_by_source.*.reported`) next to the
measurement itself for exactly that reason.

An `object (empty)` is a map keyed at run time: `usage.by_model` by model name,
`tools.by_name` by tool name, `subagents.by_id` by agent id, and
`usage.context.by_source` by context source.

## `approvals`

| path | type |
|---|---|
| `approvals.by_decision` | object (empty) |
| `approvals.decided` | number |
| `approvals.last` | null |
| `approvals.pending` | number |
| `approvals.pending_dropped` | number |
| `approvals.pending_omitted` | number |
| `approvals.pending_requests` | array |
| `approvals.requested` | number |
| `approvals.unidentified_requests` | number |
| `approvals.unmatched_decisions` | number |

## `status`

| path | type |
|---|---|
| `status.agent` | string |
| `status.close_reason` | null |
| `status.closed_at` | null |
| `status.cwd` | string |
| `status.dropped.bytes` | number |
| `status.dropped.count` | number |
| `status.entries` | number |
| `status.last_error` | null |
| `status.last_stop_reason` | null |
| `status.last_time` | null |
| `status.lifecycle` | string |
| `status.model` | string |
| `status.opened_at` | null |
| `status.owner` | string |
| `status.previous` | null |
| `status.provider` | string |
| `status.resumed` | boolean |
| `status.seeded` | boolean |
| `status.slot` | string |
| `status.turn` | null |
| `status.turn_open` | boolean |
| `status.turns_completed` | number |
| `status.turns_refused` | number |

## `subagents`

| path | type |
|---|---|
| `subagents.by_id` | object (empty) |
| `subagents.omitted` | number |
| `subagents.running` | number |
| `subagents.running_exact` | boolean |
| `subagents.totals.closed_unmatched` | number |
| `subagents.totals.completed` | number |
| `subagents.totals.failed` | number |
| `subagents.totals.spawned` | number |
| `subagents.totals.stopped` | number |
| `subagents.totals.unknown` | number |

## `timeline`

| path | type |
|---|---|
| `timeline.dropped` | number |
| `timeline.first_seq` | null |
| `timeline.last_seq` | null |
| `timeline.limit` | number |
| `timeline.moments` | array |

## `tools`

| path | type |
|---|---|
| `tools.by_name` | object (empty) |
| `tools.calls` | number |
| `tools.completed` | number |
| `tools.elapsed_ms` | number |
| `tools.errors` | number |
| `tools.names_omitted` | number |
| `tools.names_omitted_saturated` | boolean |
| `tools.open` | number |
| `tools.open_calls` | array |
| `tools.open_calls_omitted` | number |
| `tools.open_dropped` | number |
| `tools.unidentified_calls` | number |
| `tools.unmatched_completions` | number |

## `usage`

| path | type |
|---|---|
| `usage.by_model` | object (empty) |
| `usage.compactions.count` | number |
| `usage.compactions.freed_pct` | number |
| `usage.context.blocks` | number |
| `usage.context.by_source` | object (empty) |
| `usage.context.chars` | number |
| `usage.context.estimated_turns` | number |
| `usage.context.sources_omitted` | number |
| `usage.context.tokens` | number |
| `usage.context.turns` | array |
| `usage.context.window` | number |
| `usage.credits` | number |
| `usage.credits_by_source.background.credits` | number |
| `usage.credits_by_source.background.reported` | number |
| `usage.credits_by_source.subagent.credits` | number |
| `usage.credits_by_source.subagent.reported` | number |
| `usage.credits_by_source.turn.credits` | number |
| `usage.credits_by_source.turn.reported` | number |
| `usage.duration_ms` | number |
| `usage.models_omitted` | number |
| `usage.models_omitted_saturated` | boolean |
| `usage.steps.completed` | number |
| `usage.steps.ms` | number |
| `usage.tokens.cache_read` | number |
| `usage.tokens.cache_write` | number |
| `usage.tokens.input` | number |
| `usage.tokens.output` | number |
| `usage.tokens.total` | number |
| `usage.turns.completed` | number |
| `usage.turns.credits_reported` | number |
| `usage.turns.duration_reported` | number |
| `usage.turns.tokens_reported` | number |
