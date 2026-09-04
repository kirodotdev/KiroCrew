# Session Work Ledger

Owners: `kiro_crew.session_ledger`, `kiro_crew.mcp_tools.ledger`, and the dashboard ledger routes

## 1. Purpose

`kiro_crew.session_ledger` provides durable, per-session work state for long-running work. The state record holds a goal, phase, resumable next step, rejected approaches, artifact pointers, and a bounded event history. This separates resumable state from transcript context; `mcp_tools.ledger.schemas()` describes the record as authoritative over prior-cycle memory.

## 2. Storage, identity, and lifecycle

Each recorded ledger lives below `<data_home>/ledger/` in a directory containing:

```
slot_key   # original ledger key breadcrumb
state.json # complete state record
.lock      # dedicated cross-process lock inode
```

`session_ledger.record()` creates the directory and writes `state.json` atomically. `session_ledger._locked()` keeps the lock file separate from the replaced state file, so replacing state cannot let concurrent writers lock different inodes.

`session_ledger.ledger_key()` removes only dashboard namespace and prefix spellings before storage. `session_ledger._store_name()` combines a readable fold with a digest of that exact key; `test_distinct_channel_keys_never_share_a_ledger` guards against the lossy-fold collision that would otherwise let one channel session overwrite another. `session_ledger.ledger_dir()` rejects hostile raw keys and requires the resolved directory to remain below the ledger root, preventing traversal through a folded name.

Closing a dashboard tab preserves the ledger. Permanent history deletion calls `_remove_slot_for_history_key()` in `dashboard.handlers.sessions`; it cancels matching turns and destroys their sessions before calling `session_ledger.purge()` and `purge_matching()`. That ordering prevents a dying in-process turn from writing after the normal purge. `session_ledger.purge()` accepts that a cross-process race can recreate disposable orphan state; the next deletion sweep removes it.

## 3. State record and bounded writes

`session_ledger._empty_state()` defines the state fields:

| Field | Meaning |
|---|---|
| `schema` | stored schema marker |
| `goal` | binding objective |
| `phase` | current free-form work phase |
| `next` | concrete resumable intent |
| `tried` | rejected approaches with their reason and timestamp |
| `artifacts` | string pointers to work artifacts |
| `events` | recent classified progress entries |
| timestamps | creation, latest progress, and terminal-completion time |

`session_ledger._coerce_state()` supplies defaults for malformed known fields and preserves unknown fields for forward compatibility. `session_ledger._read_state_unlocked()` treats unreadable, malformed, undecodable, or oversized state as empty rather than failing a turn. `test_read_state_malformed_oversized_or_undecodable_reads_empty` pins that behavior.

`session_ledger.record()` applies partial updates. Omitted fields retain their stored value; artifact updates merge with the stored map; a supplied rejected approach appends to `tried`; and a nonblank event appends to `events`. `test_record_roundtrip_and_partial_update`, `test_artifacts_merge_and_clamp`, `test_tried_appends_and_caps`, and `test_events_tail_bounded` pin the retention and aging rules. The bounds keep a resumed session from accumulating unbounded durable context.

Every accepted record advances `last_progress_at`. Changing `phase` requires a nonblank event and a recognized event kind. `session_ledger.record()` writes the phase and its event in one atomic state document, so a crash cannot expose a phase move without its classified reason; `test_phase_and_event_land_in_one_document` enforces this load-bearing audit trail. A phase in `session_ledger.TERMINAL_PHASES` stamps `finished_at`; a later non-terminal phase clears it, as guarded by `test_terminal_phase_sets_finished_at_and_reopening_clears_it`.

`session_ledger._serialize_bounded()` evicts oldest history before an accepted state file can exceed the reader ceiling. `test_writer_guarantees_the_read_ceiling_for_legitimate_records` ensures a valid record remains readable, and `test_oversized_unknown_fields_are_dropped_not_self_corrupting` ensures oversized forward-compatible fields do not destroy known state.

Each eviction discards data that never reaches disk, so it cannot be recovered from the stored file the way a read-side clamp can. `_serialize_bounded()` therefore logs one warning per over-budget serialization naming the ledger, the evicted `events`/`tried` counts, and any unknown fields dropped - including on the refusal path, which raises with the in-memory record already stripped. The line describes the document the call built rather than a durable write, because `atomic_write` runs afterwards and may still fail. A document that fits logs nothing. `TestBoundedWriteIsLoudAboutLoss` pins the counts, the named ledger, the refusal report, the failed-write case leaving the stored file intact, and the silence.

`record()` returns the same dict `_serialize_bounded()` evicts from, so a caller's post-write view is the document on disk rather than the pre-eviction one. `test_record_returns_exactly_what_landed_on_disk` pins that equality across an eviction; serializing a copy instead would report entries the write dropped.

Writes use the bounded exclusive lock in `session_ledger._locked()`. Contention raises `OSError` rather than allowing an unserialized write or indefinitely blocking a worker; `test_record_fails_closed_on_held_lock` enforces this. Reads are lock-free because `atomic_write` exposes either the previous or complete replacement document, so the state and event tail come from one transaction.

## 4. MCP surface and authorization

`mcp_tools.__init__.DOMAIN_MODULES` registers `mcp_tools.ledger`. `session_ledger_read` has no arguments and returns the calling session's state and recent event tail. `session_ledger_record` accepts only optional state fields; `validation.SESSION_LEDGER_RECORD_SCHEMA` validates their types and lengths, while `session_ledger.record()` enforces the conditional phase/event rule.

`mcp_tools.ledger._strict_session_key()` obtains a gateway-authored session identity before either tool calls the loopback routes. This is load-bearing because the lenient resolver can walk a subagent process tree to its parent; rejecting an unverified identity prevents a subagent from reading or overwriting the parent's ledger. `test_mcp_tools_refuse_without_strict_identity` and `test_mcp_tools_pass_the_verified_key_to_transport` enforce that boundary.

`dashboard.handlers.session_ledger._resolve_ledger_key()` derives storage identity from the recognized `X-Session-Key`, never the request body. The routes reject missing or unrecognized identities and restricted session modes, so a request can read or write only its own durable ledger. `api_session_ledger_record()` sends bounded-lock failures back as retryable service errors and validates that artifact maps contain only strings. `test_route_refuses_unrecognized_session`, `test_route_refuses_restricted_session`, and `test_route_rejects_non_string_artifacts` cover those boundaries.

## 5. Auto-nudge injection

`dashboard.handlers.autonudge.compose_nudge_body()` renders the normal nudge body, then reads `render_snapshot()` in a worker thread. A nonempty, non-terminal ledger snapshot prefixes the nudge; an absent ledger, terminal phase, or snapshot exception leaves the nudge body unchanged. `test_compose_nudge_body_prefixes_snapshot`, `test_compose_nudge_body_unchanged_without_ledger`, and `test_compose_nudge_body_survives_snapshot_failure` enforce those outcomes.

`session_ledger.render_snapshot()` includes current goal, phase, next step, recent rejected approaches, and artifact pointers, and applies its own rendering bounds. It omits terminal records because completed work provides no next-cycle steering; `test_snapshot_empty_without_ledger_or_when_terminal` and `test_snapshot_contains_state_and_is_capped` guard this behavior.

Every `_fire_*_nudge` adapter in `slack.gateway` calls `compose_nudge_body()`, including the messaging and dashboard transports. `test_gateway_fire_callbacks_use_the_composer` enumerates adapters rather than pinning their count, so a new transport cannot silently bypass the ledger snapshot.

## 6. Failure behavior and scope

A read failure yields an empty ledger. A write lock failure is retryable. Permanent deletion removes matching ledger directories, while the documented cross-process race may leave only disposable orphan state for the next sweep. Snapshot failures are best-effort and never prevent the nudge from firing.

The ledger does not journal individual tool operations, arbitrate execution ownership with leases, or add a dashboard UI. It records state between wakes; the MCP tools and nudge composer are its public surfaces.
