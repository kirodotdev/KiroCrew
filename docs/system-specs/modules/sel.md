# Security Event Log (SEL) Module

Last Updated: 2026-07-13 (added `config_bounds_clamped` event + `clamped` outcome and ACP worker-pool per-tool audit; prior: `acp`, `token_auth`, and `refresh_tokens` audit sources)

## Overview

Immutable, tamper-evident audit trail for all tool invocations, MCP calls, and dashboard API mutations. Implements transactional event logging per Amazon Security Event Logging Standard.

See also the SEL section in [`security.md`](security.md) for the threat-model view of these events.

Storage: `~/.kirocrew/security_events.jsonl` (append-only JSONL with HMAC-SHA256 chain).

## Event Schema

Each entry records:

| Field | Description |
|-------|-------------|
| `event_id` | Unique 16-char hex identifier |
| `timestamp` | ISO 8601 UTC |
| `event_type` | `tool_invocation`, `api_access`, `config_bounds_clamped`, `governance_decision`, `governance_degraded` |
| `caller_identity` | Session key (e.g. `dashboard:abc`, `cron:xyz`, `subagent:123`) |
| `agent` | Agent name (`kirocrew`, custom agent name) |
| `source` | Interface: `slack`, `dashboard`, `cli`, `cron`, `subagent`, `taskrunner`, `mcp`, `background`, `acp` (ACP-transport events, e.g. `tool_interrupted`), `token_auth` / `refresh_tokens` (dashboard auth), `host` (the `_host` sentinel — an in-process host action like app activation / workspace admission), `unknown` (empty/unrecognized session key, which must NOT be mis-tagged `slack`) |
| `operation` | Tool name or `METHOD /api/path` |
| `tool_kind` | Tool category (`execute_bash`, `fs_write`, `mcp_core`, `mcp_cron`, etc.) |
| `outcome` | `invoked`, `auto_approved`, `approved`, `rejected`, `denied`, `completed`, `failed`, `clamped`, `degraded` (a governance chokepoint failed OPEN) |
| `resources` | Affected resources summary (truncated to 500 chars) |
| `downstream_service` | MCP server name if applicable (`kirocrew-core`, `kirocrew-cron`, `internal-mcp`) |
| `request_id` | ACP permission request ID |
| `error` | Error message if failed/denied |
| `prev_hash` | HMAC of previous entry (chain link) |
| `entry_hash` | HMAC-SHA256 of this entry |
| `metadata` | Additional context (approval reason, step index, etc.) |

The `config_bounds_clamped` event (`outcome=clamped`, `source=background`, `operation=config.load`, `caller_identity=config_loader`) is emitted by `config/loader.py`'s `_log_config_clamp_event` when an out-of-range security-bounded knob (`agent.subagent_auto_max` / `agent.max_subagents` / `agent.subagent_max_turns` / `session.pool_size`) is clamped to its API-enforced ceiling at load time, recording `metadata` `{file_value, clamped_to, min, max}`. Best-effort: a SEL failure never makes config loading raise.

## Integrity

- HMAC-SHA256 chain: each entry signs over the previous entry's hash
- HMAC key: `~/.kirocrew/sel_hmac.key` (32 random bytes, `chmod 600`)
- **Key + log are on the sensitive-path floor (`cdf82704`):** both `sel_hmac.key` and `security_events.jsonl` are in `security._SENSITIVE_HOME_DIRS`, so the audited agent's `fs_read`/file-edit tools (gated by `is_sensitive_path()`) cannot read the key to forge the chain or rewrite the log. The gateway's own writer/reader (`sel.py`, `dashboard/session_health.py`) opens the files directly and bypasses that gate. Residual: the key still lives in the agent's home namespace — a deeper out-of-process signer is future hardening.
- Verification: `verify_integrity()` walks the chain and reports tampered entries
- Append-only: no in-place edits; pruning rewrites with chain rebuild

## Async Writer

`log()` is off the hot path: callers enqueue the event on an unbounded
`queue.Queue` (never blocking) and a single daemon writer thread drains it,
computing the HMAC chain in enqueue order and batching up to `_QUEUE_DRAIN_BATCH`
events into one `open()`+write. The writer starts lazily on first `log()` and
registers an `atexit` flush.

- **Durability**: eventually-durable, not synchronously-durable — a crash/kill
  can lose at most the events still queued. Acceptable for an audit log; the
  hot path (e.g. per-message skill triggering) no longer pays fsync/lock latency.
- **Read-after-write**: `flush()` runs before every read path (`recent`,
  `verify_integrity`, `prune`) and on exit. It waits on a pending-event counter
  (a `threading.Condition`, race-free vs a bare queue-empty check), bounded by
  `_FLUSH_TIMEOUT_SECS` so a wedged writer can't hang a read.
- **Fallback**: if the writer can't be started, `log()` writes synchronously so
  an event is never silently dropped.
- **`sync=True`**: `SecurityEventLog(base_dir=..., sync=True)` writes each event
  inline (no thread) — used by tests that read the raw JSONL immediately after
  logging.

## Retention

Default 365 days. Pruned daily by heartbeat service (`_PRUNE_TICKS`).

## Integration Points

| Surface | What's Logged | Module |
|---------|---------------|--------|
| Slack handler | `tool_call` (invoked/denied), `permission_request` (all outcomes) | `slack/handler.py` |
| Dashboard chat | `tool_call` (invoked), `permission_request` (all outcomes) | `dashboard/chat.py` |
| TaskRunner | Permission requests during decomposition and step execution | `taskrunner.py` |
| Subagent | Permission requests during subagent execution | `subagent.py` |
| Background tasks | Permission requests via `_resolve_permission()` | `llm_helpers.py` |
| MCP core tools | `spawn_run`, `learn_add`, `task_run` calls and outcomes | `mcp_core.py` |
| MCP cron tools | `cron_add`, `cron_remove`, etc. calls and outcomes | `mcp_cron.py` |
| Dashboard API | All POST/PUT/DELETE operations via middleware | `dashboard/server.py` |
| ACP worker-pool audit | Per-`tool_call` `auto_approved` `tool_invocation` (`source=subagent`), bounded by `_SEL_AUDIT_TIMEOUT_SECONDS` (5.0s) and offloaded off the event loop so a wedged SEL backend never gates dispatch. Two emitters: the knowledge LLMPool via `AcpClient._maybe_audit_tool_call` (gated on the `audit_source` ctor param, offloaded to `subprocess_executor()`); and **code-review-sage's ReviewPool**, which migrated to the shared `AcpRuntime` (no `audit_source`) and re-emits the same per-tool record itself | `acp/client.py`, `apps/builtins/code_review_sage/sage_lib/review_pool.py` |
| Token auth | `internal_auth`, `app_scope_check`, `dashboard_sessions_revoked`, `refresh_token_initial_mint`, `nonce_evicted` (`source=token_auth`) | `dashboard/token_auth.py` |
| Refresh tokens | `refresh_token_use`, `refresh_token_logout`, `access_cookie_revoked` (`source=refresh_tokens`) | `dashboard/handlers/auth_refresh.py` |
| ACP transport | `tool_interrupted` per-turn cancellation audit (`source=acp`) | `acp/client.py` |

## APIs

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/sel/events?limit=N` | Recent security events (max 1000) |
| GET | `/api/sel/verify` | HMAC chain integrity check |

## CLI

```
kirocrew security events [-n 20]   # Show recent events
kirocrew security verify            # Verify HMAC chain integrity
```

## Thread Safety

Singleton pattern. The chain state (`_last_hash`) and the file append are
guarded by `threading.Lock`, held only inside the writer thread (and the
synchronous fallback / `prune`), never by enqueuing callers. Enqueue is
lock-free via the thread-safe `queue.Queue`. Safe for concurrent access from the
asyncio event loop + MCP server stdio processes.
